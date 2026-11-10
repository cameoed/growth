#!/bin/bash
# Private application library; never replace Ubuntu's system SQLite.
set -euo pipefail
test "$(id -u)" -eq 0
export DEBIAN_FRONTEND=noninteractive
install -d -o root -g root -m 0755 /opt/beings-growth/lib
if env LD_LIBRARY_PATH=/opt/beings-growth/lib python3 -c \
    'import sqlite3; assert sqlite3.sqlite_version == "3.53.4"' 2>/dev/null; then
    exit 0
fi
apt-get install -y --no-install-recommends gcc libc6-dev
python3 - <<'PY'
import hashlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import urllib.request
import zipfile

url = "https://www.sqlite.org/2026/sqlite-amalgamation-3530400.zip"
expected = "67f423e9ebbbdc473cbc4772c872ee6b89f31fde4ed0279a5c25d5f65c043a16"
with urllib.request.urlopen(url, timeout=120) as response:
    archive = response.read()
with zipfile.ZipFile(io.BytesIO(archive)) as archive:
    source = archive.read("sqlite-amalgamation-3530400/sqlite3.c")
if hashlib.sha3_256(source).hexdigest() != expected:
    raise RuntimeError("Official SQLite source checksum does not match")
with tempfile.TemporaryDirectory(prefix="beings-sqlite-build-") as temporary:
    work = Path(temporary)
    (work / "sqlite3.c").write_bytes(source)
    output = work / "libsqlite3.so.0"
    subprocess.run([
        "gcc", "-O2", "-fPIC", "-shared", "-DSQLITE_THREADSAFE=1",
        "-DSQLITE_ENABLE_FTS5", "-DSQLITE_ENABLE_FTS4", "-DSQLITE_ENABLE_RTREE",
        "-DSQLITE_ENABLE_MATH_FUNCTIONS", "-DSQLITE_ENABLE_COLUMN_METADATA",
        "-DSQLITE_ENABLE_DBSTAT_VTAB", "-Wl,-soname,libsqlite3.so.0",
        str(work / "sqlite3.c"), "-o", str(output), "-lm", "-ldl", "-lpthread",
    ], check=True)
    destination = Path("/opt/beings-growth/lib/libsqlite3.so.0")
    pending = destination.with_suffix(".pending")
    try:
        with output.open("rb") as source, pending.open("xb") as target:
            import shutil

            shutil.copyfileobj(source, target)
            target.flush()
            os.fsync(target.fileno())
        os.chmod(pending, 0o755)
        os.replace(pending, destination)
    finally:
        pending.unlink(missing_ok=True)
PY
env LD_LIBRARY_PATH=/opt/beings-growth/lib python3 - <<'PY'
import sqlite3

assert sqlite3.sqlite_version == "3.53.4"
with sqlite3.connect(":memory:") as connection:
    connection.execute("CREATE VIRTUAL TABLE evidence USING fts5(content)")
    connection.execute("INSERT INTO evidence VALUES ('synthetic')")
    assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert connection.execute("SELECT json_extract('{\"n\":3}', '$.n')").fetchone() == (3,)
print("Private SQLite 3.53.4: verified source, JSON, FTS5 and integrity")
PY
