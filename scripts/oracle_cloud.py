#!/usr/bin/env python3
"""Oracle VM deployment and private connection settings."""

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path

from beings_leads import oracle_deployment as deployment
from beings_leads.file_lock import open_directory_lock
from beings_leads.oracle_deployment import (
    DEFAULT_CONFIG,
    NEXT_STORAGE_CONFIG,
    REMOTE_DB,
    REMOTE_ROOT,
    REMOTE_RUNTIME_ENV,
    Remote,
    read_config,
)

LOCAL_TRANSFER_FILE = ".leads-local-transfer.db"
LOCAL_TRANSFER_HEADROOM = 1024**3


def _write_storage_preference(destination: Path, value: dict) -> None:
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


def _remote_database_command(remote: Remote, script: str) -> list[str]:
    return remote.ssh(
        f"sudo -n -u beings-growth {REMOTE_RUNTIME_ENV} {REMOTE_ROOT}/.venv/bin/python -c "
        + shlex.quote(script) + f" {shlex.quote(REMOTE_DB)} {LOCAL_TRANSFER_HEADROOM}"
    )


def _local_database_exists(path: Path) -> bool:
    return any(os.path.lexists(str(path) + suffix) for suffix in ("", "-wal", "-shm", "-journal"))


def local_transfer_status(remote: Remote, directory: Path) -> dict:
    command = _remote_database_command(remote, """
import sqlite3, sys
from pathlib import Path
with sqlite3.connect(Path(sys.argv[1]).as_uri() + '?mode=ro', uri=True) as source:
    source.execute('BEGIN')
    pages = source.execute('PRAGMA page_count').fetchone()[0]
    size = source.execute('PRAGMA page_size').fetchone()[0]
    print(pages * size)
""")
    try:
        reply = subprocess.run(command, capture_output=True, check=True, timeout=25)
        size = int(reply.stdout)
        if size <= 0:
            raise ValueError
    except (OSError, ValueError, subprocess.SubprocessError):
        raise ValueError("Could not check the VM database size over SSH.") from None
    blocked = ""
    if (directory / LOCAL_TRANSFER_FILE).exists():
        blocked = "A download is staged. Reopen Beings Growth to apply it."
    elif _local_database_exists(directory / "leads.db"):
        blocked = "A local database already exists. Switching would replace it."
    return {
        "download_bytes": size,
        "available_bytes": shutil.disk_usage(directory).free,
        "required_bytes": size + LOCAL_TRANSFER_HEADROOM,
        "blocked_reason": blocked,
    }


_STREAM_LOCAL_DATABASE = """
import hashlib, shutil, sqlite3, sys, tempfile
from contextlib import closing
from pathlib import Path

path = Path(sys.argv[1])
with tempfile.TemporaryDirectory(prefix='.local-transfer-', dir=path.parent) as folder:
    snapshot = Path(folder) / 'snapshot.db'
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as source:
        with closing(sqlite3.connect(snapshot)) as target:
            source.execute('BEGIN')
            pages = source.execute('PRAGMA page_count').fetchone()[0]
            size = source.execute('PRAGMA page_size').fetchone()[0]
            if shutil.disk_usage(path.parent).free < pages * size + int(sys.argv[2]):
                raise OSError('Insufficient temporary space on the VM')
            source.backup(target, pages=2048)
            target.execute('PRAGMA journal_mode=DELETE').fetchone()
    output = sys.stdout.buffer
    output.write(str(snapshot.stat().st_size).encode() + b'\\n')
    output.flush()
    digest = hashlib.sha256()
    with snapshot.open('rb') as database:
        while block := database.read(4 * 1024 * 1024):
            digest.update(block)
            output.write(block)
    output.write(digest.hexdigest().encode() + b'\\n')
    output.flush()
"""


def _receive_local_database(remote: Remote, output, directory: Path) -> None:
    command = _remote_database_command(remote, _STREAM_LOCAL_DATABASE)
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as child:
        try:
            header = child.stdout.readline(32)
            if not re.fullmatch(rb"[1-9][0-9]*\n", header):
                raise ValueError("Could not download the VM database.")
            remaining = int(header)
            if shutil.disk_usage(directory).free < remaining + LOCAL_TRANSFER_HEADROOM:
                raise ValueError("Free space dropped below the database download requirement.")
            digest = hashlib.sha256()
            while remaining:
                block = child.stdout.read(min(remaining, 4 * 1024 * 1024))
                if not block:
                    raise ValueError("The database download ended early. Try again.")
                output.write(block)
                digest.update(block)
                remaining -= len(block)
            checksum = child.stdout.readline(66)
            if checksum != digest.hexdigest().encode() + b"\n" or child.wait():
                raise ValueError("Could not verify the downloaded database. Try again.")
        finally:
            if child.poll() is None:
                child.kill()


def download_local_database(config: Path, pending: Path) -> None:
    import sqlite3

    from beings_leads.storage_json import configure

    directory = config.parent
    staged = directory / LOCAL_TRANSFER_FILE
    try:
        lock = open_directory_lock(directory, blocking=False)
    except BlockingIOError:
        raise ValueError("Storage is busy. Wait for the current operation to finish.") from None
    try:
        remote = read_config(config)
        check = local_transfer_status(remote, directory)
        if check["blocked_reason"]:
            raise ValueError(check["blocked_reason"])
        if check["available_bytes"] < check["required_bytes"]:
            raise ValueError("There is not enough free space to download the VM database.")
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".local-transfer-") as output:
            _receive_local_database(remote, output, directory)
            output.flush()
            os.fsync(output.fileno())
            downloaded = Path(output.name)
            try:
                with closing(sqlite3.connect(downloaded.as_uri() + "?immutable=1", uri=True)) as db:
                    configure(db, rows=False)
                    verified = db.execute("PRAGMA quick_check(1)").fetchone() == ("ok",)
            except sqlite3.DatabaseError:
                verified = False
            if not verified:
                raise ValueError("The downloaded SQLite database failed verification.")
            if read_config(config) != remote or _local_database_exists(directory / "leads.db"):
                raise ValueError("Storage changed during the download. Try again.")
            size = downloaded.stat().st_size
            inode = downloaded.stat().st_ino
            os.link(downloaded, staged)
            try:
                _write_storage_preference(pending, {
                    "mode": "local", "download": {"bytes": size, "inode": inode},
                })
            except BaseException:
                staged.unlink()
                raise
    finally:
        os.close(lock)


def _activate_local_download(directory: Path, receipt: dict) -> Path:
    staged = directory / LOCAL_TRANSFER_FILE
    database = directory / "leads.db"

    def verified(path):
        if path.is_symlink() or not path.is_file():
            return False
        stat = path.stat()
        return receipt == {"bytes": stat.st_size, "inode": stat.st_ino}

    if verified(database):
        return staged
    if _local_database_exists(database) or not verified(staged):
        raise ValueError("The verified download could not be activated. Check local storage.")
    os.link(staged, database)
    return staged


def apply_local_download(pending: Path, config: Path) -> bool:
    if not pending.exists():
        return False
    value = json.loads(pending.read_text())
    if not isinstance(value, dict) or value.get("mode") != "local" or "download" not in value:
        return False
    try:
        lock = open_directory_lock(config.parent, blocking=False)
    except BlockingIOError:
        raise ValueError("Stop the local dashboard and try again.") from None
    try:
        if json.loads(pending.read_text()) != value:
            raise ValueError("The storage preference changed. Try reopening Beings Growth.")
        if config.exists():
            read_config(config)
        staged = _activate_local_download(config.parent, value["download"])
        if config.exists():
            config.chmod(0o600)
            os.replace(config, config.with_name("oracle-vm-profile.json"))
        staged.unlink(missing_ok=True)
        pending.unlink()
        return True
    finally:
        os.close(lock)


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        if command in {"check-local-download", "download-local"}:
            parser = argparse.ArgumentParser(prog=f"oracle_cloud.py {command}")
            parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
            config = parser.parse_args(sys.argv[2:]).config.absolute()
            if command == "check-local-download":
                print(json.dumps(local_transfer_status(read_config(config), config.parent)))
            else:
                download_local_database(config, config.with_name(NEXT_STORAGE_CONFIG.name))
            return
        if command == "apply-storage" and apply_local_download(NEXT_STORAGE_CONFIG, DEFAULT_CONFIG):
            return
        if command in {"stage-oracle", "stage-local"} and (
            NEXT_STORAGE_CONFIG.with_name(LOCAL_TRANSFER_FILE).exists()
        ):
            raise ValueError("Reopen Beings Growth to apply the downloaded local database first.")
    except (OSError, TypeError, ValueError) as exc:
        message = str(exc) if type(exc) is ValueError else "Could not update storage settings."
        raise SystemExit(message) from None
    deployment.main()


if __name__ == "__main__":
    main()
