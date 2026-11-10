"""Bridge an authenticated operator SSH session to Oracle's private ranking socket."""

import os
import socket
import sys
from contextlib import suppress
from threading import Thread

SOCKET_PATH = "/run/beings-growth/ranking.sock"


def main():
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        try:
            connection.connect(SOCKET_PATH)
        except (FileNotFoundError, ConnectionRefusedError):
            return  # An older or disconnected service may not have a ranking listener.

        def upstream():
            try:
                while chunk := os.read(sys.stdin.fileno(), 64 * 1024):
                    connection.sendall(chunk)
            except OSError:
                pass
            finally:
                with suppress(OSError):
                    connection.shutdown(socket.SHUT_WR)

        Thread(target=upstream, daemon=True).start()
        try:
            with connection.makefile("rb", buffering=0) as stream:
                while chunk := stream.read(64 * 1024):
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()
        except BrokenPipeError:
            pass


if __name__ == "__main__":
    main()
