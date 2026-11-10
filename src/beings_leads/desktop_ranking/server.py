"""Oracle's private ranking listener. It plans, reads and commits without scoring."""

import logging
import os
import time
from contextlib import contextmanager, suppress
from pathlib import Path
from socketserver import StreamRequestHandler
from threading import Thread

from ..sales_engine.maintenance import _set_ranking_stage
from ..store import LeadStore
from . import control
from .jobs import MissionWindows, Plan
from .reads import authorize_read, write_queries
from .wire import VERSION, ProtocolError, Wire, code_fingerprint

logger = logging.getLogger(__name__)
# Bound open WAL snapshots and release ownership on stalled/disconnected clients.
SESSION_IDLE_SECONDS = 12
SNAPSHOT_SECONDS = 60
MAX_WRITE_BATCH = 8


class RankingHandler(StreamRequestHandler):
    def setup(self):
        self.request.settimeout(SESSION_IDLE_SECONDS)
        super().setup()

    def handle(self):
        state = self.server.state
        wire = Wire(self.rfile, self.wfile)
        plan = None
        request = None
        owned = False
        try:
            greeting = wire.receive()
            if greeting[:3] == ["submit", VERSION, code_fingerprint()]:
                self._submit(state, wire, greeting[3:])
                return
            if greeting != ["hello", VERSION, code_fingerprint()]:
                wire.send(["incompatible"])
                return
            if not state.owner.acquire(blocking=False):
                wire.send(["occupied"])
                return
            owned = True
            if not state.connect(self.request):
                return
            wire.send(["ready", VERSION])
            _set_ranking_stage("waiting")
            with (
                LeadStore(state.database, initialize=False) as writer,
                LeadStore(state.database, initialize=False, read_only=True) as reader,
            ):
                windows = MissionWindows()
                deadline = 0
                while True:
                    command, *arguments = wire.receive()
                    if command == "next":
                        if plan is not None:
                            raise ProtocolError("Unfinished ranking plan")
                        request = state.next_request()
                        if request is None and state.should_defer():
                            _set_ranking_stage("smart_scan")
                            wire.send({"wait": 1})
                            continue
                        _set_ranking_stage("selecting")
                        plan = Plan(writer, request, windows)
                        if plan.tasks:
                            reader.connection.execute("BEGIN")
                            # Pin the snapshot immediately after the prepared identity writes.
                            reader.connection.execute("SELECT 1 FROM sqlite_master LIMIT 1")
                            deadline = time.monotonic() + SNAPSHOT_SECONDS
                            reader.connection.set_authorizer(authorize_read)
                            self._stage(plan)
                            wire.send(
                                {
                                    "tasks": plan.work(),
                                    "as_of": plan.now.isoformat(),
                                    "domains": plan.domains,
                                }
                            )
                            continue
                        result = self._finish(plan)
                        plan = request = None
                        wire.send({"wait": 0.02 if result["busy"] else 0.5})
                    elif command == "queries" and plan is not None:
                        if time.monotonic() > deadline:
                            raise TimeoutError("Ranking read snapshot expired")
                        reader.connection.set_progress_handler(
                            lambda deadline=deadline: int(time.monotonic() > deadline),
                            100_000,
                        )
                        write_queries(reader.connection, wire, arguments[0])
                    elif command == "publish" and plan is not None:
                        if time.monotonic() > deadline:
                            raise TimeoutError("Ranking read snapshot expired")
                        (packets,) = arguments
                        if not 1 <= len(packets) <= MAX_WRITE_BATCH:
                            raise ProtocolError("Invalid ranking publication batch")
                        plan.publish(packets)
                        self._stage(plan)
                        wire.send({"accepted": len(packets), **plan.result()})
                    elif command == "finish" and plan is not None:
                        if plan.tasks.keys() != plan.finished.keys():
                            raise ProtocolError("Ranking plan is incomplete")
                        reader.connection.set_authorizer(None)
                        reader.connection.set_progress_handler(None, 0)
                        reader.connection.rollback()
                        result = self._finish(plan)
                        plan = request = None
                        wire.send(result)
                    elif command == "ping":
                        wire.send({"alive": True})
                    elif command == "bye":
                        return
                    else:
                        raise ProtocolError("Unexpected ranking command")
        except (EOFError, ConnectionError, TimeoutError):
            pass
        except Exception:
            logger.exception("Desktop ranking session interrupted")
        finally:
            if owned:
                state.disconnect(request)
                _set_ranking_stage("waiting_desktop")
                state.owner.release()

    @staticmethod
    def _submit(state, wire, arguments):
        kind, parameters = arguments
        if kind not in {"refresh", "candidates", "mission", "reproject", "target"}:
            raise ProtocolError("Unknown ranking request")
        for key in ("candidate_ids", "company_keys"):
            if len(parameters.get(key) or ()) > control.MAX_COMPANIES:
                raise ProtocolError("Ranking request exceeds the company bound")
        try:
            wire.send(state.submit(kind, parameters))
        except control.DesktopRankingUnavailable as exc:
            wire.send({"unavailable": str(exc)})

    @staticmethod
    def _stage(plan):
        if plan.shared and plan.checked_shared < plan.total_shared:
            _set_ranking_stage("shared_rankings", shared=(plan.checked_shared, plan.total_shared))
            return
        active = {
            mission["id"]
            for task in plan.tasks.values()
            if task["key"] not in plan.finished
            for mission, _ in task.get("missions", [])
        }
        if len(active) == 1:
            _set_ranking_stage("scoring", next(iter(active)))
        elif active:
            _set_ranking_stage("scoring_missions")
        else:
            _set_ranking_stage("ranking_views")

    @staticmethod
    def _finish(plan):
        result = plan.result()
        if plan.finished:
            logger.info(
                "Desktop ranking published shared=%s mission=%s lenses=%s projections=%s "
                "discarded=%s errors=%s",
                result["shared_companies_refreshed"],
                result["mission_refreshed"],
                result["lens_refreshed"],
                len(result["reprojected"]),
                result["discarded"],
                len(result["errors"]),
            )
        if plan.request is not None:
            plan.request.complete(result)
        _set_ranking_stage("waiting")
        return result


@contextmanager
def serve_ranking(database, socket, should_defer):
    """Own a private listener for this service lifecycle."""
    from socketserver import ThreadingUnixStreamServer

    database, socket = Path(database).resolve(), Path(socket)
    with LeadStore(database, initialize=False) as store:
        store.connection.execute("""
            CREATE TABLE IF NOT EXISTS engine_desktop_projection_pending(
                candidate_id INTEGER PRIMARY KEY REFERENCES candidates(id) ON DELETE CASCADE,
                enqueued_at TEXT NOT NULL, due_at TEXT NOT NULL, failures INTEGER NOT NULL DEFAULT 0
            )
        """)
        store.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_desktop_projection_due "
            "ON engine_desktop_projection_pending(due_at,candidate_id)"
        )
        store.connection.commit()
    state = control.RankingControl(database, should_defer)
    if socket.parent.stat().st_mode & 0o077:
        raise RuntimeError("The desktop ranking socket requires a private runtime directory")
    # systemd supplies a mode-0700 parent owned by the runtime user.
    with ThreadingUnixStreamServer(str(socket), RankingHandler) as server:
        server.daemon_threads = True
        server.state = state
        socket.chmod(0o600)
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        control._active = state
        _set_ranking_stage("waiting_desktop")
        thread.start()
        try:
            yield state
        finally:
            control._active = None
            state.close()
            server.shutdown()
            thread.join()
            state.disconnect()
            with suppress(FileNotFoundError):
                socket.unlink()


def configured_socket(database):
    from ..oracle_deployment import REMOTE_DB

    value = os.environ.get("BEINGS_DESKTOP_RANKING_SOCKET")
    return Path(value) if value and database.resolve() == Path(REMOTE_DB) else None
