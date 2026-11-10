"""Deploy the existing application privately on an Oracle Ubuntu VM.

This is operator tooling, not a database driver or a background backup job. Production
transfer requires a stopped local backend and explicit migration-backup authorization.
No command deletes the local original or relaxes SSH host verification.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import logging
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass
from pathlib import Path

from .file_lock import open_directory_lock
from .release_mode import USER_MISSION_MARKER, requires_user_mission

ROOT = Path(__file__).resolve().parents[2]
REMOTE_ROOT = "/opt/beings-growth"
REMOTE_DB = f"{REMOTE_ROOT}/data/leads.db"
REMOTE_RUNTIME_ENV = f"env LD_LIBRARY_PATH={REMOTE_ROOT}/lib"
SQLITE_VERSION = "3.53.4"
logger = logging.getLogger(__name__)
DEFAULT_CONFIG = ROOT / "data/oracle-remote.json"
NEXT_STORAGE_CONFIG = DEFAULT_CONFIG.with_name("oracle-storage-next.json")
PACKAGE_ROOTS = (
    "pyproject.toml",
    "LICENSE",
    "README.md",
    "src",
    "config/disallowed_domains.txt",
    "docs/validated-crt-prospecting-domains.md",
    "deploy/oracle",
    "scripts/microlink_discovery.py",
)


def assert_local_store_allowed(path: Path) -> None:
    """Keep legacy local commands from forking inventory after Oracle cutover."""
    if not (ROOT / "data/oracle-remote.json").exists():
        return
    if path.resolve() != (ROOT / "data/leads.db").resolve():
        return
    # Presence is intentional: native remote mode also fails closed on malformed
    # or disabled configuration instead of silently reopening local storage.
    raise RuntimeError(
        "Oracle remote mode is configured. Use the app's private API or run "
        "this command on the Oracle server; local database access is disabled."
    )


@dataclass(frozen=True)
class Remote:
    host: str
    identity_file: str
    known_hosts_file: str
    user: str = "ubuntu"
    ssh_port: int = 22
    local_port: int = 18787

    def __post_init__(self):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", self.host):
            raise ValueError("Expected an Oracle VM hostname or IPv4 address")
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", self.user):
            raise ValueError("Invalid SSH user")
        if type(self.ssh_port) is not int or not 1 <= self.ssh_port <= 65535:
            raise ValueError("Invalid SSH port")
        if type(self.local_port) is not int or not 1024 <= self.local_port <= 65535:
            raise ValueError("Invalid local port")
        if self.local_port == 8787:
            raise ValueError("Remote mode must use a separate local port (default 18787)")
        for filename in (self.identity_file, self.known_hosts_file):
            if not isinstance(filename, str):
                raise ValueError("SSH identity and known-hosts paths must be absolute")
            path = Path(filename)
            if not path.is_absolute() or any(c in path.as_posix() for c in '\n\r\0"\\$'):
                raise ValueError("SSH identity and known-hosts paths must be absolute")

    def ssh_options(self) -> list[str]:
        known_hosts = Path(self.known_hosts_file).as_posix().replace("%", "%%")
        return [
            "-F",
            "none",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "GlobalKnownHostsFile=none",
            "-o",
            "UpdateHostKeys=no",
            "-o",
            f'UserKnownHostsFile="{known_hosts}"',
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "ForwardAgent=no",
            "-o",
            "PermitLocalCommand=no",
            "-o",
            "ConnectTimeout=15",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
            "-i",
            Path(self.identity_file).as_posix(),
            "-p",
            str(self.ssh_port),
        ]

    def ssh(self, command: str) -> list[str]:
        return ["ssh", "-T", *self.ssh_options(), "--", f"{self.user}@{self.host}", command]

    def tunnel(self, *, local_socket: Path | None = None) -> list[str]:
        listener = str(local_socket) if local_socket is not None else f"127.0.0.1:{self.local_port}"
        return [
            "ssh",
            "-N",
            "-T",
            *self.ssh_options(),
            "-o",
            "ExitOnForwardFailure=yes",
            "-L",
            f"{listener}:127.0.0.1:8787",
            "--",
            f"{self.user}@{self.host}",
        ]


def parse_config(value: dict) -> Remote:
    if not isinstance(value, dict):
        raise ValueError("Expected an Oracle connection profile")
    value = value.copy()
    if value.pop("enabled", False) is not True:
        raise ValueError("Remote configuration is not enabled")
    if value.pop("remote_database", REMOTE_DB) != REMOTE_DB:
        raise ValueError("Unexpected remote database path")
    return Remote(**value)


def read_config(path: Path) -> Remote:
    return parse_config(json.loads(path.read_text(encoding="utf-8-sig")))


def connection_profile(remote: Remote) -> dict:
    return {**asdict(remote), "enabled": True, "remote_database": REMOTE_DB}


def verify_connection(remote: Remote) -> None:
    """Verify the user's key, pinned host and private HTTP service over SSH."""
    for filename in (remote.identity_file, remote.known_hosts_file):
        if not Path(filename).is_file():
            raise ValueError("Choose existing SSH identity and trusted known-hosts files")
    command = [
        "ssh", "-T", *remote.ssh_options(), "-W", "127.0.0.1:8787",
        "--", f"{remote.user}@{remote.host}",
    ]
    try:
        if forwarded_health(command) == {"status": "ok", "database": REMOTE_DB}:
            return
    except (OSError, ValueError, http.client.HTTPException):
        raise ValueError(
            "Could not reach the private service. Check SSH keys, host trust and TCP forwarding."
        ) from None
    raise ValueError("The private Beings Growth service is unavailable on the VM")


def forwarded_health(command: list[str]) -> dict | None:
    # Keep SSH stdin open while reading. Closing it early closes the direct TCP channel.
    client, child = socket.socketpair()
    with client, child, subprocess.Popen(
        command, stdin=child, stdout=child, stderr=subprocess.DEVNULL,
    ) as process:
        child.close()
        client.settimeout(20)
        try:
            client.sendall(
                b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1:8787\r\nConnection: close\r\n\r\n"
            )
            with http.client.HTTPResponse(client) as response:
                response.begin()
                return json.loads(response.read(4096)) if response.status == 200 else None
        finally:
            if process.poll() is None:
                process.kill()


def stage_storage(remote: Remote | None, destination: Path = NEXT_STORAGE_CONFIG) -> None:
    if remote is not None:
        verify_connection(remote)
    value = connection_profile(remote) if remote else {"mode": "local"}
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", dir=destination.parent, delete=False) as file:
        pending = Path(file.name)
        try:
            json.dump(value, file)
            file.flush()
            os.fsync(file.fileno())
            os.replace(pending, destination)
        finally:
            pending.unlink(missing_ok=True)


def apply_storage(
    pending: Path = NEXT_STORAGE_CONFIG, destination: Path = DEFAULT_CONFIG,
) -> None:
    """Apply an explicit preference on launch, before starting a new local service."""
    if not pending.exists():
        return
    try:
        descriptor = open_directory_lock(destination.parent, blocking=False)
    except BlockingIOError:
        raise ValueError("Stop the local dashboard and try again.") from None
    try:
        try:
            value = json.loads(pending.read_text())
        except FileNotFoundError:
            return
        if value == {"mode": "local"}:
            try:
                read_config(destination)
            except (OSError, TypeError, ValueError):
                destination.unlink(missing_ok=True)
            else:
                # A profile is connection metadata, not a copy of either store.
                # Keep it out of the active path so local mode can open normally.
                destination.chmod(0o600)
                os.replace(destination, destination.with_name("oracle-vm-profile.json"))
            pending.unlink()
        else:
            parse_config(value)
            os.replace(pending, destination)
    finally:
        os.close(descriptor)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def inspect_connection(connection, path: Path) -> dict:
    """Exhaustive SQLite checks, schema identity, exact counts and physical checksum."""
    logger.info("Checking full SQLite integrity")
    integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
    if integrity != ["ok"]:
        raise ValueError("Database integrity check failed: " + "; ".join(integrity[:5]))
    logger.info("Checking foreign keys")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("Database foreign-key check failed")
    schema = [
        tuple(row)
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        )
    ]
    logger.info("Checking schema and exact counts of every physical table")
    counts = {}
    for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall():
        name = row[0]
        quoted = '"' + name.replace('"', '""') + '"'
        counts[name] = connection.execute(f"SELECT count(*) FROM {quoted}").fetchone()[0]
    logger.info("Checking complete database checksum")
    return {
        "version": 1,
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "schema_sha256": hashlib.sha256(json.dumps(schema).encode()).hexdigest(),
        "table_counts": counts,
        "integrity": "ok",
        "foreign_key_violations": 0,
    }


def inspect_database(path: Path) -> dict:
    from .store import LeadStore

    with LeadStore(path, initialize=False, read_only=True, maintenance=True) as store:
        store.connection.execute("BEGIN")
        return inspect_connection(store.connection, path)


@contextmanager
def frozen_database(path: Path):
    """Hold existing maintenance ownership and a SQLite write lock for the transfer."""
    from .store import LeadStore

    path = path.resolve(strict=True)
    try:
        descriptor = open_directory_lock(path.parent, blocking=False)
    except BlockingIOError as exc:
        raise RuntimeError(
            "Stop the dashboard and all database workers before migration"
        ) from exc
    try:
        with LeadStore(path, initialize=False, maintenance=True) as store:
            result = store.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if result[0]:
                raise RuntimeError("Database is busy; no transfer was started")
            store.connection.execute("BEGIN IMMEDIATE")
            wal = Path(str(path) + "-wal")
            if wal.exists() and wal.stat().st_size:
                raise RuntimeError("Uncheckpointed writes appeared; no transfer was started")
            yield inspect_connection(store.connection, path)
    finally:
        os.close(descriptor)


def package_members(root: Path = ROOT):
    """Allowlist only runtime code and policy; never archive data or local secrets."""
    optional = (USER_MISSION_MARKER,) if requires_user_mission(root) else ()
    for relative in (*PACKAGE_ROOTS, *optional):
        base = root / relative
        if not base.exists():
            raise FileNotFoundError(f"Required deployment input is missing: {relative}")
        candidates = sorted(base.rglob("*")) if base.is_dir() else [base]
        for path in candidates:
            rel = path.relative_to(root)
            if path.is_symlink():
                raise ValueError(f"Deployment inputs cannot be symlinks: {rel}")
            if any(part.startswith(".") or part == "__pycache__" for part in rel.parts):
                continue
            if path.is_file() and path.suffix not in {".pyc", ".db", ".pem", ".key"}:
                yield path, rel.as_posix()


def run_remote(remote: Remote, command: str, *, data: bytes | None = None, capture=False):
    try:
        return subprocess.run(remote.ssh(command), input=data, check=True, capture_output=capture)
    except subprocess.CalledProcessError as exc:
        if capture and exc.stderr:
            sys.stderr.write(exc.stderr.decode("utf-8", errors="replace"))
        raise


def runtime_check_command() -> str:
    return (
        f"sudo {REMOTE_RUNTIME_ENV} {REMOTE_ROOT}/.venv/bin/python -c "
        + shlex.quote(f"import sqlite3; assert sqlite3.sqlite_version == {SQLITE_VERSION!r}")
    )


def install(remote: Remote):
    # The SSH operator cannot traverse the runtime user's private data directory.
    run_remote(remote, f"sudo test ! -e {REMOTE_DB}")
    # Bootstrap does not start a service or create a database.
    run_remote(remote, "sudo bash -s", data=(ROOT / "deploy/oracle/bootstrap.sh").read_bytes())
    members = list(package_members())
    command = (
        f"sudo -u beings-growth test ! -e {REMOTE_DB} && "
        f"sudo -u beings-growth tar -xmf - -C {REMOTE_ROOT}"
    )
    with subprocess.Popen(remote.ssh(command), stdin=subprocess.PIPE) as process:
        try:
            with tarfile.open(fileobj=process.stdin, mode="w|") as archive:
                for path, name in members:
                    archive.add(path, arcname=name, recursive=False)
        finally:
            process.stdin.close()
        if process.wait():
            raise RuntimeError("Source upload failed; the service was not started")
    run_remote(
        remote,
        (
            f"sudo bash {REMOTE_ROOT}/deploy/oracle/sqlite-runtime.sh && "
            f"sudo -u beings-growth python3 -m venv {REMOTE_ROOT}/.venv && "
            f"sudo -u beings-growth {REMOTE_ROOT}/.venv/bin/pip install -e {REMOTE_ROOT} && "
            f"sudo install -m 0644 {REMOTE_ROOT}/deploy/oracle/beings-growth.service "
            "/etc/systemd/system/beings-growth.service && sudo systemctl daemon-reload"
        ),
    )


def publish_database(incoming: Path, active: Path, backup: Path, expected: dict, *, owner=None):
    """Retain one verified migration backup and atomically publish an independent file."""
    descriptor = open_directory_lock(active.parent, blocking=False)
    try:
        return _publish_database(incoming, active, backup, expected, owner=owner)
    finally:
        os.close(descriptor)


def _publish_database(incoming: Path, active: Path, backup: Path, expected: dict, *, owner=None):
    if active.exists():
        raise FileExistsError("Refusing to replace an existing active database")
    if len({incoming.resolve(), active.resolve(), backup.resolve()}) != 3:
        raise ValueError("Incoming, backup and active database must be separate paths")
    if any(path.is_symlink() for path in (incoming, active, backup)):
        raise ValueError("Database publication paths cannot be symlinks")
    if incoming.stat().st_size != expected["size_bytes"]:
        raise ValueError("Transferred database size does not match")
    logger.info("Checking uploaded database checksum")
    if sha256_file(incoming) != expected["sha256"]:
        raise ValueError("Transferred database checksum does not match")
    actual = inspect_database(incoming)
    if actual != expected:
        raise ValueError("Transferred database manifest does not match")
    # Both are on one Oracle volume. Do not consume a second full copy until free space is checked.
    required = incoming.stat().st_size + 1024**3
    if shutil.disk_usage(active.parent).free < required:
        raise RuntimeError("Oracle volume needs space for the active database plus 1 GiB headroom")
    logger.info("Full uploaded database manifest verified")
    pending = active.with_name(active.name + ".pending")
    created_pending = False
    try:
        # Publish the verified backup without replacement; detach the upload name immediately
        # so a resumed rsync can never modify the backup through a shared inode.
        if backup.exists():
            if sha256_file(backup) != expected["sha256"]:
                raise FileExistsError("Existing migration backup has different contents")
        else:
            os.link(incoming, backup)
        incoming.unlink()
        os.chmod(backup, 0o600)
        if owner is not None:
            os.chown(backup, *owner)
        with backup.open("rb") as saved_backup:
            os.fsync(saved_backup.fileno())
        directory_fd = os.open(backup.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        logger.info("Creating independent active database from verified migration backup")
        with backup.open("rb") as source, pending.open("xb") as destination:
            created_pending = True
            shutil.copyfileobj(source, destination, 4 * 1024 * 1024)
            destination.flush()
            os.fsync(destination.fileno())
        os.chmod(pending, 0o600)
        if owner is not None:
            os.chown(pending, *owner)
        logger.info("Checking independent active database checksum")
        if sha256_file(pending) != expected["sha256"]:
            raise ValueError("Active database copy checksum does not match")
        os.link(pending, active)
        pending.unlink()
        created_pending = False
        descriptor = os.open(active.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if created_pending:
            pending.unlink(missing_ok=True)
    logger.info("Active database and migration backup published")
    return actual


def transfer(remote: Remote, database: Path, *, authorized_backup: bool):
    if not authorized_backup:
        raise ValueError("Migration backup requires --create-migration-backup")
    preflight = (
        runtime_check_command() + " && "
        f"sudo -u beings-growth test ! -e {REMOTE_DB} && "
        "! systemctl is-active --quiet beings-growth"
    )
    run_remote(remote, preflight)
    print("Acquiring database ownership, checkpointing WAL and checking full integrity", flush=True)
    with frozen_database(database) as manifest:
        print("Uploading the frozen database; the local original remains intact", flush=True)
        ssh = shlex.join(["ssh", *remote.ssh_options()])
        subprocess.run(
            [
                "rsync",
                "--partial",
                "--inplace",
                "--checksum",
                "--progress",
                "-e",
                ssh,
                "--",
                str(database.resolve()),
                f"{remote.user}@{remote.host}:{REMOTE_ROOT}/incoming/leads.db",
            ],
            check=True,
        )
        command = (
            f"cd {REMOTE_ROOT} && sudo {REMOTE_RUNTIME_ENV} .venv/bin/python -m "
            "beings_leads.oracle_deployment publish --create-migration-backup"
        )
        result = run_remote(remote, command, data=json.dumps(manifest).encode(), capture=True)
        receipt = json.loads(result.stdout)
        if receipt != manifest:
            raise ValueError("Oracle publication receipt does not match the local source")
        print(json.dumps({"status": "verified", "manifest": receipt}), flush=True)
    return manifest


def start(remote: Remote, *, empty: bool = False):
    if empty:
        initialize = shlex.quote(
            "from pathlib import Path\n"
            "from beings_leads.starter_inventory import initialize_starter_inventory\n"
            f"initialize_starter_inventory(Path({REMOTE_DB!r}))"
        )
        prepare = (
            f"(sudo -u beings-growth test -s {REMOTE_DB} || "
            f"sudo -u beings-growth {REMOTE_RUNTIME_ENV} "
            f"{REMOTE_ROOT}/.venv/bin/python -c {initialize})"
        )
    else:
        prepare = f"sudo -u beings-growth test -s {REMOTE_ROOT}/data/oracle-migration-backup.db"
    run_remote(
        remote,
        (
            runtime_check_command() + " && "
            + prepare + " && "
            f"sudo -u beings-growth test -s {REMOTE_DB} && "
            "sudo systemctl enable --now beings-growth"
        ),
    )
    # This only reads health; source collection remains under the existing app controls.
    return run_remote(
        remote,
        (
            f"{REMOTE_ROOT}/.venv/bin/python -c "
            + shlex.quote(
                "import json,time,urllib.request\n"
                "for attempt in range(180):\n"
                " try:\n"
                "  value=json.load(urllib.request.urlopen('http://127.0.0.1:8787/healthz',timeout=2))\n"
                f"  assert value['database']=={REMOTE_DB!r}\n"
                "  print(json.dumps(value));break\n"
                " except Exception:\n"
                "  if attempt==179:raise\n"
                "  time.sleep(1)\n"
            )
        ),
        capture=True,
    )


@contextmanager
def tunnel_ranking_worker(config: Path | None):
    if config is None:
        yield
        return
    with subprocess.Popen(
        [sys.executable, "-m", "beings_leads.desktop_ranking.worker", str(config)],
        stdin=subprocess.PIPE,
    ) as worker:
        try:
            yield
        finally:
            with suppress(OSError):
                worker.stdin.close()
            try:
                worker.wait(timeout=3)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait()


def open_tunnel(remote: Remote, *, ranking_config: Path | None = None):
    # OpenSSH rejects an occupied listener with ExitOnForwardFailure. A separate
    # raw bind is racy and incorrectly rejects recently closed TIME_WAIT sockets.
    command = remote.tunnel()
    if os.name != "nt" and ranking_config is None:
        os.execvp(command[0], command)
        return
    try:
        with tunnel_ranking_worker(ranking_config):
            status = subprocess.run(command, check=False).returncode
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    if status:
        raise SystemExit(status)


def enable_remote(remote: Remote, destination: Path = DEFAULT_CONFIG):
    """Publish connection settings only after remote startup/health succeeds."""
    value = connection_profile(remote)
    if destination.exists():
        if json.loads(destination.read_text()) != value:
            raise FileExistsError("An existing remote connection cannot be silently replaced")
        start(remote)
        return
    start(remote)
    destination.parent.mkdir(parents=True, exist_ok=True)
    pending = destination.with_name(destination.name + ".pending")
    created = False
    try:
        with pending.open("x") as output:
            created = True
            json.dump(value, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(pending, 0o600)
        os.link(pending, destination)
    finally:
        if created:
            pending.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "install",
            "transfer",
            "start",
            "start-empty",
            "enable",
            "tunnel",
            "publish",
            "stage-oracle",
            "stage-local",
            "apply-storage",
        ),
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--database", type=Path, default=ROOT / "data/leads.db")
    parser.add_argument("--create-migration-backup", action="store_true")
    parser.add_argument("--stdin", action="store_true", help="Read stage-oracle profile from stdin")
    parser.add_argument("--desktop-ranking", action="store_true",
                        help="Run rankings on this computer while the VM tunnel is open")
    args = parser.parse_args()
    if args.desktop_ranking and args.command != "tunnel":
        parser.error("--desktop-ranking requires the tunnel command")
    if args.command in {"stage-oracle", "stage-local", "apply-storage"}:
        try:
            if args.command == "stage-oracle":
                try:
                    profile = json.load(sys.stdin) if args.stdin else json.loads(
                        args.config.read_text(),
                    )
                    remote = parse_config(profile)
                except (TypeError, ValueError, OSError):
                    raise ValueError("Invalid Oracle connection profile.") from None
                stage_storage(remote)
            elif args.command == "stage-local":
                stage_storage(None)
            else:
                apply_storage()
        except (OSError, TypeError, ValueError) as exc:
            message = str(exc) if type(exc) is ValueError else "Could not update storage settings."
            print(message, file=sys.stderr)
            sys.exit(1)
        return
    if args.command == "publish":
        if not args.create_migration_backup or os.getuid() != 0:
            parser.error("Remote publication requires sudo and --create-migration-backup")
        import pwd
        import sqlite3

        if sqlite3.sqlite_version != SQLITE_VERSION:
            parser.error(f"Remote publication requires the private SQLite {SQLITE_VERSION} runtime")
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
        logger.info("Publication process %s; SQLite %s", os.getpid(), sqlite3.sqlite_version)

        account = pwd.getpwnam("beings-growth")
        expected = json.load(sys.stdin)
        receipt = publish_database(
            Path(f"{REMOTE_ROOT}/incoming/leads.db"),
            Path(REMOTE_DB),
            Path(f"{REMOTE_ROOT}/data/oracle-migration-backup.db"),
            expected,
            owner=(account.pw_uid, account.pw_gid),
        )
        print(json.dumps(receipt))
        return
    remote = read_config(args.config)
    if args.command == "install":
        install(remote)
    elif args.command == "transfer":
        transfer(remote, args.database, authorized_backup=args.create_migration_backup)
    elif args.command == "start":
        print(start(remote).stdout.decode())
    elif args.command == "start-empty":
        print(start(remote, empty=True).stdout.decode())
    elif args.command == "tunnel":
        open_tunnel(remote, ranking_config=args.config if args.desktop_ranking else None)
    elif args.command == "enable":
        enable_remote(remote)


if __name__ == "__main__":
    main()
