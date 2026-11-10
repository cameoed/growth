"""Verify the source release against an isolated, provider-free dashboard."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread, Timer
from types import SimpleNamespace
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from beings_leads.desktop_ranking.control import RankingControl
from beings_leads.desktop_ranking.control import Request as RankingRequest
from beings_leads.desktop_ranking.server import RankingHandler
from beings_leads.desktop_ranking.wire import VERSION, Wire, code_fingerprint
from beings_leads.desktop_ranking.worker import Session, run_session
from beings_leads.file_lock import open_directory_lock
from beings_leads.oracle_deployment import read_config
from beings_leads.sales_engine.ranking import refresh_candidate_rankings
from beings_leads.scan_resources import resource
from beings_leads.store import LeadStore

STARTER_HOSTS = {"google.com", "youtube.com", "bloomberg.com"}


def check_locks(directory: Path) -> None:
    database = directory / "leads.db"
    shared = open_directory_lock(directory, shared=True)
    try:
        second_reader = open_directory_lock(directory, shared=True, blocking=False)
        os.close(second_reader)
        try:
            exclusive = open_directory_lock(directory, blocking=False)
        except BlockingIOError:
            pass
        else:
            os.close(exclusive)
            raise AssertionError("Maintenance acquired a lock while readers were active")
    finally:
        os.close(shared)
    os.close(open_directory_lock(directory, blocking=False))

    # Acquiring and releasing the full lane covers the platform byte-lock API.
    with resource("cpu", weight=4, database=database):
        pass
    with resource("cpu", weight=4, database=database):
        pass


def check_dashboard(directory: Path) -> None:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("BEINGS_", "OPENAI_", "APIFY_", "DATAFORSEO_",
                                  "MICROLINK_", "TREG_", "TIKTOK_"))}
    env.update(BEINGS_LEADS_PORT="0", PYTHONUTF8="1")
    database = directory / "leads.db"
    opened = directory / "opened.json"
    script = (
        "import json\n"
        "from importlib.metadata import entry_points\n"
        "from pathlib import Path\n"
        "from urllib.request import urlopen\n"
        "from beings_leads import web\n"
        f"web.DEFAULT_DB = Path({str(database)!r})\n"
        f"opened = Path({str(opened)!r})\n"
        "def open_browser(url):\n"
        "    with urlopen(url + 'healthz', timeout=10) as reply:\n"
        "        health = json.load(reply)\n"
        "    pending = opened.with_suffix('.pending')\n"
        "    pending.write_text(json.dumps([url, health]), encoding='utf-8')\n"
        "    pending.replace(opened)\n"
        "async def skip_website_scan(request, *args, **kwargs):\n"
        "    assert request.only_hosts == ['parity-smoke.example.com']\n"
        "    return {'status': 'queued'}\n"
        "web.webbrowser.open = open_browser\n"
        "web.scan_manager.start = skip_website_scan\n"
        "next(iter(entry_points(group='console_scripts', name='beings-growth'))).load()()\n"
    )

    def get(path, payload=None):
        body = None if payload is None else json.dumps(payload).encode()
        request = Request(address + path.lstrip("/"), data=body,
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=10) as reply:
            return json.load(reply)

    with subprocess.Popen([sys.executable, "-c", script], env=env) as server:
        try:
            for _ in range(120):
                if server.poll() is not None:
                    raise RuntimeError(f"Dashboard exited with status {server.returncode}")
                if opened.exists():
                    address, health = json.loads(opened.read_text(encoding="utf-8"))
                    break
                time.sleep(0.25)
            else:
                raise RuntimeError("The launcher did not open a ready dashboard")
            assert address.startswith("http://127.0.0.1:")
            assert health["status"] == "ok" and Path(health["database"]) == database.resolve()
            duplicate = subprocess.run(
                [sys.executable, "-c", script],
                env={**env, "BEINGS_LEADS_PORT": str(urlsplit(address).port)},
                timeout=15, capture_output=True, text=True, check=False,
            )
            assert duplicate.returncode and "Could not start the dashboard" in duplicate.stderr
            assert "Waiting for application startup" not in duplicate.stderr
            assert get("/api/sales-engine/missions")["items"] == []
            draft = get("/api/sales-engine/missions", {
                "name": "Digital media",
                "brief": "Media companies that produce video and digital content",
                "defer_ai": True,
            })
            mission = get("/api/sales-engine/missions")["items"][0]
            assert mission["id"] == draft["id"]
            assert mission["compilation"]["error_code"] == "configure_openai_key"
            ranking = get(f"/api/sales-engine/overview?mission_id={mission['id']}&compact=true")
            assert ranking["scoring_available"] is False
            inventory = get("/api/sales-engine/overview?mission_id=all&compact=true")
            assert inventory["total"] == len(STARTER_HOSTS)
            assert {row["company_key"] for row in inventory["items"]} == STARTER_HOSTS
            candidate = inventory["items"][0]["candidate_id"]
            saved = get("/api/sales-engine/outbound/manual", {
                "candidate_id": candidate, "name": "Zoë Test",
            })
            contacts = get(f"/api/sales-engine/outbound/company/{candidate}")["items"]
            assert any(row["id"] == saved["id"] and row["name"] == "Zoë Test" for row in contacts)
            assert get("/api/smart-scan?fast=true")["status"] == "idle"
            imported = get("/api/scans/import", {"urls": "https://parity-smoke.example.com"})
            assert imported["accepted"] == 1 and imported["status"] == "queued"
        finally:
            server.terminate()
            server.wait(timeout=15)


def check_ssh_profile(directory: Path) -> None:
    credentials = directory / "SSH profile with spaces"
    credentials.mkdir()
    identity = credentials / "key"
    identity.touch()
    hosts = credentials / "known%hosts"
    profile = directory / "oracle.json"
    profile.write_text(json.dumps({
        "enabled": True, "host": "192.0.2.1", "identity_file": str(identity),
        "known_hosts_file": str(hosts),
    }), encoding="utf-8-sig")
    remote = read_config(profile)
    ssh = Path(os.environ["SYSTEMROOT"]) / "System32/OpenSSH/ssh.exe" if os.name == "nt" else "ssh"
    args = [str(ssh), "-G", *remote.tunnel()[1:]]
    lines = subprocess.run(args, check=True, capture_output=True, text=True).stdout.splitlines()
    assert f"identityfile {identity.as_posix()}" in lines
    assert f"userknownhostsfile {hosts.as_posix()}" in lines
    assert "stricthostkeychecking true" in lines
    assert "globalknownhostsfile none" in lines
    assert "localforward 127.0.0.1:18787 127.0.0.1:8787" in [
        line.replace("[127.0.0.1]", "127.0.0.1") for line in lines
    ]


def check_desktop_ranking(directory: Path) -> None:
    database = directory / "leads.db"
    now = datetime.now(UTC)
    ranks = "SELECT * FROM engine_company_ranking ORDER BY company_key"
    with LeadStore(database) as store:
        keys = store.candidate_ids(sorted(STARTER_HOSTS))
        assert refresh_candidate_rankings(store, keys.values(), now=now) == len(STARTER_HOSTS)
        expected = [tuple(row) for row in store.connection.execute(ranks)]
        store.connection.execute("DELETE FROM engine_company_ranking")
        store.connection.commit()
    state = RankingControl(database, lambda: True)
    request = RankingRequest(
        "candidates", {"candidate_ids": list(keys.values()), "now": now.isoformat()}
    )
    state.requests.put_nowait(request)
    client, upstream = socket.socketpair()
    with client, upstream, client.makefile("rb") as reader, client.makefile("wb") as writer:
        server = Thread(target=RankingHandler, args=(upstream, None, SimpleNamespace(state=state)))
        server.start()
        client.settimeout(12)
        session = Session(Wire(reader, writer))
        stopping = Event()
        timeout = Timer(30, stopping.set)
        timeout.start()
        try:
            assert session.call("hello", VERSION, code_fingerprint()) == ["ready", VERSION]
            path = None if os.name == "nt" else str(directory / "relay")
            run_session(session, path, stopping, workers=2, on_result=lambda _: stopping.set())
            result = request.future.result(timeout=1)
            assert not result["errors"], result
            assert result["shared_companies_refreshed"] == len(STARTER_HOSTS), result
        finally:
            timeout.cancel()
            client.shutdown(socket.SHUT_RDWR)
            server.join(timeout=15)
            session.close()
    with LeadStore(database, initialize=False, read_only=True) as store:
        assert [tuple(row) for row in store.connection.execute(ranks)] == expected


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="growth-smoke-") as temp:
        check_locks(Path(temp))
        check_dashboard(Path(temp))
        check_ssh_profile(Path(temp))
        check_desktop_ranking(Path(temp))
    print("Browser launch, mission draft, import, prospects, file locks, SSH and ranking passed.")
