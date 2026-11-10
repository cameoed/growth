"""Run desktop ranking against Oracle's exact SQLite snapshot."""

from __future__ import annotations

import argparse
import base64
import json
import logging
import multiprocessing
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from contextlib import contextmanager, suppress
from datetime import datetime
from functools import cache
from pathlib import Path
from queue import Empty, Queue
from secrets import compare_digest, token_bytes
from socketserver import StreamRequestHandler, ThreadingTCPServer
from tempfile import TemporaryDirectory
from threading import BoundedSemaphore, Event, Lock, Thread

from ..domain_policy import policy_rules
from ..missions import scoring as missions
from ..missions.compute import INVENTORY_PIN_SQL, _inventory_pin, _mission_is_current
from ..missions.store import MISSION_BY_ID_SQL
from ..oracle_deployment import REMOTE_DB, REMOTE_ROOT, REMOTE_RUNTIME_ENV, read_config
from ..sales_engine import evidence_accounting, ranking, ranking_modes
from ..sales_engine.service import SalesEngineService
from ..storage_json import encode_text
from ..store import LeadStore
from .jobs import ProjectionWrites, _candidate_pin
from .reads import SnapshotConnection, receive_rows, write_rows
from .wire import VERSION, ProtocolError, Wire, code_fingerprint

logger = logging.getLogger(__name__)
WORKERS = min(8, max(1, (os.cpu_count() or 2) - 2))
WRITE_BATCH = 6
READ_FLIGHTS = 3
FLIGHT_QUERIES = 64
COALESCE_SECONDS = 0.001
FLUSH_SECONDS = 0.09


class ScoringStore(LeadStore):
    def __init__(self, connection):
        # Scoring reuses the store methods, while every SQLite read belongs to Oracle's snapshot.
        self.connection = connection
        self.path = Path(REMOTE_DB)
        self.frontier_intelligence = cache(super().frontier_intelligence)
        self.scoring_snapshot = cache(super().scoring_snapshot)


def _prepare_shared(engine, key, now):
    try:
        result = ranking._evaluate_company(engine, key, now)
        if result is not None:
            values, lenses, accounting = result
            packed = list(
                evidence_accounting.prepare_packets(
                    key,
                    accounting,
                    now=now,
                    policy=values[7],
                    evidence_version=values[8],
                )
            )
            result = (
                values,
                lenses,
                [(*row[:3], base64.b64encode(row[3]).decode("ascii")) for row in packed],
            )
        return {"result": result}
    except (ValueError, TypeError, KeyError) as exc:
        return {"error": type(exc).__name__}


def _prepare_missions(store, key, jobs, now):
    @cache
    def evidence():
        return missions.load_evidence(store, key, now)

    connection = store.connection
    connection.prefetch(
        [
            (INVENTORY_PIN_SQL, (key,)),
            *((MISSION_BY_ID_SQL, (mission["id"],)) for mission, _ in jobs),
            *(missions._review_query(row, mission, now) for mission, row in jobs),
        ]
    )
    pin = _inventory_pin(store, jobs[0][1])
    results = []
    for mission, row in jobs:
        if pin != (row["candidate_id"], row["input_version"]) or not _mission_is_current(
            store,
            mission,
        ):
            results.append({"stale": True})
            continue
        result = missions._evaluate(store, row, mission, now, evidence=evidence)
        result["audit_json"] = encode_text(result["audit_json"], "audit")
        results.append(result)
    return results


def _prepare_projection(store, cid, now):
    current = store.current_candidate_score(cid, reference_time=now)
    if current is None:
        return []
    row, candidate, observation, score = current
    projection = ProjectionWrites()
    store._upsert_snapshot_target_intelligence_uncommitted(
        snapshot_id=row["snapshot_id"],
        candidate_id=cid,
        candidate=candidate,
        observation=observation,
        score=score,
        computed_at=now.isoformat(),
        writer=projection,
    )
    return projection.commands


def _prepare_lens(connection, key):
    row = connection.execute(
        "SELECT company_key,policy_version,evidence_version,as_of,"
        "target_json,assessment_json,priority_json FROM engine_company_ranking WHERE company_key=?",
        (key,),
    ).fetchone()
    if row is None:
        return None
    return ranking_modes.prepare_lenses(
        row,
        *(json.loads(row[field]) for field in ("target_json", "assessment_json", "priority_json")),
    )


def prepare(task, store, now):
    key, output = task["key"], {}
    if "shared" in task or "target" in task:
        engine = SalesEngineService(store, initialize=False)
    if "shared" in task:
        output["shared"] = _prepare_shared(engine, key, now)
    if "missions" in task:
        output["missions"] = _prepare_missions(store, key, task["missions"], now)
    if "lens" in task:
        output["lens"] = _prepare_lens(store.connection, key)
    if "candidate" in task:
        cid = task["candidate"]
        if list(_candidate_pin(store.connection, cid)) != task["candidate_pin"]:
            output["stale"] = True
        else:
            try:
                output["projection"] = _prepare_projection(store, cid, now)
            except (ValueError, TypeError, KeyError) as exc:
                output["error"] = type(exc).__name__
    if "target" in task:
        target = task["target"]
        cid = target["candidate_id"]
        if list(_candidate_pin(store.connection, cid)) == task["candidate_pin"]:
            output["target"] = engine._current_target(cid, target["target"], now)
    return output


def evaluate_company(endpoint, task, stamp, domains):
    address, token = endpoint
    family = socket.AF_UNIX if isinstance(address, str) else socket.AF_INET
    with (
        socket.socket(family, socket.SOCK_STREAM) as connection,
        policy_rules(domains),
    ):
        connection.settimeout(65)
        connection.connect(address)
        if token is not None:
            connection.sendall(token)
        with connection.makefile("rb") as reader, connection.makefile("wb") as writer:
            wire = Wire(reader, writer, compress=False)

            def exchange(queries):
                wire.send(queries)
                return (receive_rows(wire) for _ in queries)

            store = ScoringStore(SnapshotConnection(exchange))
            return task["key"], prepare(task, store, datetime.fromisoformat(stamp))


def _initialize_process(relay_address):
    # Parallelize companies, without a nested process pool for source replays.
    os.environ["BEINGS_SYNTHESIS_WORKERS"] = "0"
    connect = socket.socket.connect

    def private_socket_only(self, address):
        if address != relay_address:
            raise RuntimeError("Ranking workers may only access the private database relay")
        return connect(self, address)

    socket.socket.connect = private_socket_only


class Session:
    def __init__(self, wire):
        self.wire = wire
        self.lock = Lock()
        self.send_lock = Lock()
        self.replies = Queue()
        self.error = None
        self.round_trips = self.sql_reads = 0
        self.read_seconds = 0.0
        self.reader = Thread(target=self._receive, daemon=True)
        self.reader.start()

    def _fail(self, error):
        with self.lock:
            if self.error is not None:
                return
            self.error = error
            pending = []
            while True:
                try:
                    _, reply = self.replies.get_nowait()
                except Empty:
                    break
                pending.append(reply)
            self.replies.put(None)
        for reply in pending:
            reply.set_exception(error)

    def close(self):
        self._fail(EOFError("Ranking connection closed"))
        self.reader.join(timeout=2)

    def _receive(self):
        # Oracle answers in request order, even when several reads are in flight.
        while (request := self.replies.get()) is not None:
            receive, reply = request
            try:
                result = receive()
            except Exception as exc:
                self._fail(exc)
                reply.set_exception(exc)
                return
            reply.set_result(result)

    def _request(self, message, receive):
        reply = Future()
        with self.send_lock:
            with self.lock:
                if self.error is not None:
                    raise self.error
                self.replies.put((receive, reply))
            try:
                self.wire.send(message)
            except Exception as exc:
                self._fail(exc)
                raise
        return reply

    def call(self, command, *arguments):
        return self._request([command, *arguments], self.wire.receive).result()

    def read_into(self, queries, consume):
        self.round_trips += 1
        self.sql_reads += len(queries)

        def receive():
            started = time.monotonic()
            for index in range(len(queries)):
                consume(index, receive_rows(self.wire))
            self.read_seconds += time.monotonic() - started

        return self._request(["queries", queries], receive)

    def read(self, queries):
        rows = []
        self.read_into(queries, lambda _, result: rows.append(result)).result()
        return rows


class BatchReads:
    """Coalesce worker queries and release each result as the stream delivers it."""

    def __init__(self, session):
        self.session = session
        self.pending = Queue()
        self.schema = {}
        self.in_flight = {}
        self.lock = Lock()
        self.slots = BoundedSemaphore(READ_FLIGHTS)
        self.thread = Thread(target=self.run, daemon=True)

    def _reserve(self, batch):
        fresh = []
        with self.lock:
            for queries, submission in batch:
                replies = []
                for sql, parameters in queries:
                    key = (sql, tuple((type(value), value) for value in parameters))
                    reply = self.schema.get(key) or self.in_flight.get(key)
                    if reply is None:
                        self.in_flight[key] = reply = Future()
                        fresh.append((key, (sql, parameters), reply))
                    replies.append(reply)
                submission.set_result(replies)
        return fresh

    def _dispatch(self, items):
        def consume(index, rows):
            key, (sql, _), reply = items[index]
            with self.lock:
                if self.in_flight.get(key) is not reply:
                    return
                del self.in_flight[key]
                metadata = sql in {
                    "PRAGMA schema_version",
                    "PRAGMA query_only",
                    "SELECT name FROM sqlite_master WHERE type='table'",
                    MISSION_BY_ID_SQL,
                } or sql.startswith("PRAGMA table_info(")
                if metadata and len(self.schema) < 128 and rows.size <= 32 * 1024:
                    self.schema[key] = reply
            reply.set_result(rows)

        def complete(flight):
            try:
                flight.result()
            except Exception as exc:
                with self.lock:
                    failed = [
                        self.in_flight.pop(key) for key, _, reply in items
                        if self.in_flight.get(key) is reply
                    ]
                for reply in failed:
                    reply.set_exception(exc)
            finally:
                self.slots.release()

        self.slots.acquire()
        try:
            flight = self.session.read_into([query for _, query, _ in items], consume)
        except Exception as exc:
            flight = Future()
            flight.set_exception(exc)
        flight.add_done_callback(complete)

    def run(self):
        while (item := self.pending.get()) is not None:
            batch = [item]
            time.sleep(COALESCE_SECONDS)
            stopping = False
            while len(batch) < WORKERS:
                try:
                    extra = self.pending.get_nowait()
                except Empty:
                    break
                if extra is None:
                    stopping = True
                    break
                batch.append(extra)
            fresh = self._reserve(batch)
            for start in range(0, len(fresh), FLIGHT_QUERIES):
                self._dispatch(fresh[start : start + FLIGHT_QUERIES])
            if stopping:
                return


class LocalHandler(StreamRequestHandler):
    def setup(self):
        self.request.settimeout(65)
        super().setup()

    def handle(self):
        wire = Wire(self.rfile, self.wfile, compress=False)
        try:
            token = self.server.token
            if token is not None:
                self.request.settimeout(2)
                if not compare_digest(self.rfile.read(len(token)), token):
                    return
                self.request.settimeout(65)
            while True:
                queries = wire.receive()
                future = Future()
                self.server.batches.pending.put((queries, future))
                for reply in future.result(timeout=62):
                    result = reply.result(timeout=62)
                    if result.error is not None:
                        wire.send({"sql_error": result.error})
                    else:
                        write_rows(wire, result.columns, result.values)
        except (EOFError, OSError, ProtocolError, ValueError):
            pass


class LocalServer(ThreadingTCPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, path, handler):
        # The Windows relay uses an ephemeral loopback port and a per-session
        # token passed only to the spawned workers. Unix keeps its private socket.
        self.token = token_bytes(32) if path is None else None
        if path is not None:
            self.address_family = socket.AF_UNIX
        self.connections = set()
        self.connections_lock = Lock()
        super().__init__(path if path is not None else ("127.0.0.1", 0), handler)

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def get_request(self):
        peer, address = super().get_request()
        with self.connections_lock:
            self.connections.add(peer)
        return peer, address

    def shutdown_request(self, peer):
        with self.connections_lock:
            self.connections.discard(peer)
        super().shutdown_request(peer)

    def close_connections(self):
        with self.connections_lock:
            for peer in self.connections:
                with suppress(OSError):
                    peer.shutdown(socket.SHUT_RDWR)


@contextmanager
def local_reads(session, path):
    batches = BatchReads(session)
    with LocalServer(path, LocalHandler) as server:
        server.batches = batches
        batches.thread.start()
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        thread.start()
        try:
            yield batches, (server.server_address, server.token)
        finally:
            server.shutdown()
            thread.join()
            server.close_connections()
            batches.pending.put(None)
            batches.thread.join(timeout=2)


def _publish(session, packets: Iterable):
    # Keep results flowing as soon as the first workers finish, batching other ready rows.
    if packets:
        response = session.call("publish", packets)
        if response["accepted"] != len(packets):
            raise ProtocolError("Incomplete ranking write acknowledgement")


def run_plan(session, executor, endpoint, plan, stopping):
    pending = {
        executor.submit(evaluate_company, endpoint, task, plan["as_of"], plan["domains"])
        for task in plan["tasks"]
    }
    batch = []
    flush_at = time.monotonic() + FLUSH_SECONDS
    keepalive_at = time.monotonic() + 4
    while pending:
        if stopping.is_set():
            raise InterruptedError("Desktop ranking is stopping")
        done, pending = wait(pending, timeout=FLUSH_SECONDS, return_when=FIRST_COMPLETED)
        for future in done:
            batch.append(future.result())
            if len(batch) == WRITE_BATCH:
                _publish(session, batch)
                batch = []
                flush_at = time.monotonic() + FLUSH_SECONDS
        if batch and time.monotonic() >= flush_at:
            _publish(session, batch)
            batch = []
            flush_at = time.monotonic() + FLUSH_SECONDS
        if time.monotonic() >= keepalive_at:
            session.call("ping")
            keepalive_at = time.monotonic() + 4
    _publish(session, batch)
    return session.call("finish")


def run_session(session, path, stopping, *, workers=WORKERS, on_result=None):
    executor = None
    try:
        with local_reads(session, path) as (reads_broker, endpoint):
            executor = ProcessPoolExecutor(
                max_workers=workers,
                mp_context=multiprocessing.get_context("spawn"),
                initializer=_initialize_process,
                initargs=(endpoint[0],),
            )
            while not stopping.is_set():
                selecting = time.monotonic()
                plan = session.call("next")
                if "wait" in plan:
                    stopping.wait(plan["wait"])
                    continue
                reads_broker.schema.clear()
                started = time.monotonic()
                rounds, reads = session.round_trips, session.sql_reads
                received, read_seconds = session.wire.bytes_received, session.read_seconds
                result = run_plan(session, executor, endpoint, plan, stopping)
                if on_result:
                    on_result(result)
                logger.info(
                    "Desktop ranking committed shared=%s missions=%s lenses=%s projections=%s "
                    "discarded=%s seconds=%.2f select_seconds=%.2f read_batches=%s sql_reads=%s "
                    "download_mib=%.2f read_seconds=%.2f",
                    result["shared_companies_refreshed"],
                    result["mission_refreshed"],
                    result["lens_refreshed"],
                    len(result["reprojected"]),
                    result["discarded"],
                    time.monotonic() - started,
                    started - selecting,
                    session.round_trips - rounds,
                    session.sql_reads - reads,
                    (session.wire.bytes_received - received) / (1024 * 1024),
                    session.read_seconds - read_seconds,
                )
    finally:
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)


@contextmanager
def ssh_session(remote):
    command = (
        f"sudo -n -u beings-growth {REMOTE_RUNTIME_ENV} "
        f"{REMOTE_ROOT}/.venv/bin/python -m beings_leads.desktop_ranking.relay"
    )
    with subprocess.Popen(remote.ssh(command), stdin=subprocess.PIPE, stdout=subprocess.PIPE) as p:
        session = Session(Wire(p.stdout, p.stdin))
        try:
            yield session
        finally:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait(timeout=2)
            session.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    remote = read_config(args.config)
    stopping = Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())

    def watch_gateway():
        # The app owns the write end. Its crash/exit also ends the scoring child.
        with suppress(OSError):
            if not os.isatty(sys.stdin.fileno()) and not os.read(sys.stdin.fileno(), 1):
                stopping.set()

    Thread(target=watch_gateway, daemon=True).start()
    delay = 1
    while not stopping.is_set():
        attached = False
        try:
            with ssh_session(remote) as session:
                hello = session.call("hello", VERSION, code_fingerprint())
                if hello == ["ready", VERSION]:
                    attached = True
                    delay = 1
                    with suppress(InterruptedError):
                        if os.name == "nt":
                            run_session(session, None, stopping)
                        else:
                            with TemporaryDirectory(prefix="beings-rank-", dir="/tmp") as directory:
                                run_session(session, str(Path(directory) / "sql"), stopping)
                elif hello == ["incompatible"]:
                    logger.warning("Desktop ranking requires matching desktop and Oracle builds")
                    delay = 15
                elif hello == ["occupied"]:
                    delay = 5
                else:
                    raise ProtocolError("Unexpected ranking handshake")
        except (EOFError, ConnectionError, TimeoutError, sqlite3.Error) as exc:
            if attached and not stopping.is_set():
                logger.warning("Desktop ranking connection interrupted: %s", exc, exc_info=True)
            delay = min(delay * 2, 15)
        except Exception:
            logger.exception("Desktop ranking session interrupted")
            delay = min(delay * 2, 15)
        stopping.wait(delay)


if __name__ == "__main__":
    main()
