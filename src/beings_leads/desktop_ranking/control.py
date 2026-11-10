"""Queue explicit ranking requests ahead of the desktop owner's background work."""

from concurrent.futures import Future, InvalidStateError, TimeoutError
from contextlib import suppress
from dataclasses import dataclass, field
from queue import Empty, Full, Queue
from socket import SHUT_RDWR
from threading import Lock

REQUEST_TIMEOUT = 90
MAX_REQUESTS = 32
MAX_COMPANIES = 24


class DesktopRankingUnavailable(RuntimeError):
    def __init__(self, message="Start the desktop ranking worker to finish ranking."):
        super().__init__(message)


@dataclass
class Request:
    kind: str
    parameters: dict
    future: Future = field(default_factory=Future)

    def complete(self, result):
        if not self.future.done():
            try:
                self.future.set_result(result)
            except InvalidStateError:
                # A caller may cancel concurrently with publication.
                if not self.future.cancelled():
                    raise


class RankingControl:
    def __init__(self, database, should_defer):
        self.database = database
        self.should_defer = should_defer
        self.owner = Lock()
        self.lock = Lock()
        self.requests = Queue(MAX_REQUESTS)
        self.connected = False
        self.peer = None
        self.closed = False

    def connect(self, peer):
        with self.lock:
            if self.closed:
                return False
            self.peer = peer
            self.connected = True
            return True

    def close(self):
        with self.lock:
            self.closed = True
            if self.peer is not None:
                with suppress(OSError):
                    self.peer.shutdown(SHUT_RDWR)
        self.disconnect()

    def submit(self, kind, parameters):
        request = Request(kind, parameters)
        with self.lock:
            if not self.connected:
                raise DesktopRankingUnavailable()
            try:
                self.requests.put_nowait(request)
            except Full as exc:
                raise DesktopRankingUnavailable(
                    "The desktop ranking queue is full. Try again."
                ) from exc
        try:
            return request.future.result(timeout=REQUEST_TIMEOUT)
        except TimeoutError as exc:
            request.future.cancel()
            raise DesktopRankingUnavailable("Desktop ranking timed out.") from exc

    def next_request(self):
        while True:
            try:
                request = self.requests.get_nowait()
            except Empty:
                return None
            if not request.future.cancelled():
                return request

    def disconnect(self, current=None):
        with self.lock:
            self.connected = False
            self.peer = None
            for request in (current, *iter(self.next_request, None)):
                if request is not None and not request.future.done():
                    try:
                        request.future.set_exception(DesktopRankingUnavailable())
                    except InvalidStateError:
                        if not request.future.cancelled():
                            raise


_active = None


def require_connected_owner(database):
    active = _active
    if active is not None and database.resolve() == active.database:
        with active.lock:
            if not active.connected:
                raise DesktopRankingUnavailable()


class SocketControl:
    def __init__(self, path):
        self.path = path

    def submit(self, kind, parameters):
        import socket

        from .wire import VERSION, Wire, code_fingerprint

        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
                peer.settimeout(REQUEST_TIMEOUT + 1)
                peer.connect(self.path)
                with peer.makefile("rb") as reader, peer.makefile("wb") as writer:
                    wire = Wire(reader, writer)
                    wire.send(["submit", VERSION, code_fingerprint(), kind, parameters])
                    result = wire.receive()
        except (OSError, EOFError) as exc:
            raise DesktopRankingUnavailable() from exc
        if "unavailable" in result:
            raise DesktopRankingUnavailable(result["unavailable"])
        return result


def control_for(store):
    path = getattr(store, "path", None)
    if path is None:
        return None
    active = _active
    if active is not None and path.resolve() == active.database:
        return active

    from ..oracle_deployment import REMOTE_DB
    from .relay import SOCKET_PATH

    if str(path) == REMOTE_DB:
        from pathlib import Path

        if Path(SOCKET_PATH).is_socket():
            return SocketControl(SOCKET_PATH)
    return None


def submit(store, control, kind, **parameters):
    if store.connection.in_transaction:
        raise RuntimeError("Commit ranking inputs before requesting desktop scoring")
    return control.submit(kind, parameters)


def reproject(store, control, candidate_ids, *, now=None):
    """Preserve requests from completed collection while the desktop worker is offline."""
    from datetime import UTC, datetime

    from ..sales_engine.persistence import SalesEngineRepository

    if store.connection.in_transaction:
        raise RuntimeError("Commit evidence before queuing a desktop projection")
    ids = list(dict.fromkeys(int(cid) for cid in candidate_ids))
    if not ids:
        return set()
    stamp = datetime.now(UTC).isoformat()
    with SalesEngineRepository(store.connection)._transaction():
        store.connection.executemany(
            "INSERT INTO engine_desktop_projection_pending(candidate_id,enqueued_at,due_at) "
            "SELECT id,?,? FROM candidates WHERE id=? ON CONFLICT(candidate_id) DO UPDATE SET "
            "enqueued_at=excluded.enqueued_at,due_at=excluded.due_at,failures=0",
            ((stamp, stamp, cid) for cid in ids),
        )
    completed = set()
    for start in range(0, len(ids), MAX_COMPANIES):
        try:
            result = submit(
                store,
                control,
                "reproject",
                candidate_ids=ids[start : start + MAX_COMPANIES],
                now=now.isoformat() if now is not None else None,
            )
        except DesktopRankingUnavailable:
            break  # The durable rows remain due.
        completed.update(result["reprojected"])
    return completed


def reproject_candidates(store, candidate_ids):
    if (control := control_for(store)) is not None:
        return reproject(store, control, candidate_ids)
    return {cid for cid in candidate_ids if store.reproject_candidate_intelligence(cid)}
