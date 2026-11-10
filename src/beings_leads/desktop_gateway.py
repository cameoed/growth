"""Serve the Mac app's bundled dashboard and relay its API over pinned SSH."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from starlette.responses import Response

from .local_access import DESKTOP_API_VERSION, require_local
from .oracle_deployment import REMOTE_DB, Remote, read_config

UPSTREAM = httpx.URL("http://127.0.0.1:8787")
PROOF_HEADER = "X-Beings-Desktop-Gateway"
REQUEST_HEADERS = {
    "accept", "content-type", "content-length", "if-none-match", "if-modified-since",
    "x-mission-request-id",
}
RESPONSE_HEADERS = {
    "cache-control", "content-disposition", "content-encoding", "content-length",
    "content-type", "etag", "last-modified", "retry-after", "x-mission-request-id",
}
ASSETS = {
    "app.js", "sales-engine.js", "company-website.js", "styles.css",
    "beings-growth-logo.png", "beings-star.png",
}


def create_gateway(dashboard: Path, port: int, proof: str, client: httpx.AsyncClient) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    origin = f"http://127.0.0.1:{port}"
    index = dashboard.joinpath("index.html").read_text(encoding="utf-8")
    public_install: bool | None = None
    checked_version: str | None = None
    health_lock = asyncio.Lock()

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        try:
            require_local(request)
            if request.headers.get("host") != f"127.0.0.1:{port}" or (
                request.headers.get("sec-fetch-site") in {"cross-site", "same-site"}
            ):
                raise HTTPException(403, "Open the Beings Growth app to use this service.")
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return await call_next(request)

    def status(code: int, message: str):
        return JSONResponse(
            {"status": message}, status_code=code,
            headers={PROOF_HEADER: proof, "Cache-Control": "no-store"},
        )

    @app.get("/healthz")
    async def health():
        nonlocal public_install, checked_version
        async with health_lock:
            try:
                reply = await client.get("/healthz", timeout=2)
                info = reply.json()
                if (
                    reply.status_code != 200 or not isinstance(info, dict)
                    or info.get("status") != "ok" or info.get("database") != REMOTE_DB
                ):
                    public_install = None
                    return status(503, "vm_unavailable")
                version = reply.headers.get("X-Beings-API-Version")
                if version is not None and version != DESKTOP_API_VERSION:
                    public_install = None
                    return status(426, "api_incompatible")
                if public_install is None or version != checked_version:
                    probe = await client.get("/api/sales-engine/missions", timeout=3)
                    if probe.status_code >= 500:
                        public_install = None
                        return status(503, "vm_unavailable")
                    missions = probe.json()
                    if (
                        probe.status_code != 200 or not isinstance(missions, dict)
                        or not isinstance(missions.get("items"), list)
                        or "default_mission_id" not in missions
                    ):
                        public_install = None
                        return status(426, "api_incompatible")
                    public_install = missions["default_mission_id"] != "beings"
                    checked_version = version
            except (httpx.HTTPError, ValueError):
                public_install = None
                return status(503, "vm_unavailable")
        return JSONResponse(info, headers={PROOF_HEADER: proof, "Cache-Control": "no-store"})

    @app.get("/")
    async def home():
        if public_install is None:
            return HTMLResponse("Loading…", status_code=503)
        attributes = 'data-storage-mode="vm"'
        if public_install:
            attributes += " data-require-user-mission"
        return HTMLResponse(
            index.replace("<html ", f"<html {attributes} ", 1),
            headers={"Cache-Control": "no-store", PROOF_HEADER: proof},
        )

    @app.get("/assets/{name}")
    async def asset(name: str):
        if name not in ASSETS:
            raise HTTPException(404, "Dashboard asset unavailable")
        return FileResponse(dashboard / name, headers={"Cache-Control": "no-store"})

    @app.api_route("/api/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])
    async def api(path: str, request: Request):
        nonlocal public_install
        if public_install is None:
            raise HTTPException(503, "Loading…")
        if not path or any(part in {".", ".."} for part in path.split("/")):
            raise HTTPException(404, "API route unavailable")
        raw_path = request.scope["raw_path"]
        query = request.scope["query_string"]
        url = UPSTREAM.copy_with(raw_path=raw_path + (b"?" + query if query else b""))
        headers = {k: v for k, v in request.headers.items() if k in REQUEST_HEADERS}
        headers["accept-encoding"] = "identity"
        if request.headers.get("origin") == origin:
            headers["origin"] = str(UPSTREAM)
        body = request.stream() if (
            request.headers.get("content-length") not in {None, "0"}
            or "transfer-encoding" in request.headers
        ) else None
        try:
            upstream = await client.send(
                client.build_request(request.method, url, headers=headers, content=body),
                stream=True,
            )
        except httpx.HTTPError:
            public_install = None
            raise HTTPException(503, "The VM connection was interrupted.") from None
        response_headers = {k: v for k, v in upstream.headers.items() if k in RESPONSE_HEADERS}
        response_headers["x-content-type-options"] = "nosniff"
        response_headers["content-security-policy"] = "default-src 'none'; sandbox"
        if upstream.status_code == 304 or request.method == "HEAD":
            await upstream.aclose()
            return Response(status_code=upstream.status_code, headers=response_headers)
        if 300 <= upstream.status_code < 400:
            await upstream.aclose()
            raise HTTPException(502, "The VM returned an unexpected API redirect.")

        async def relay():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()

        return StreamingResponse(
            relay(), status_code=upstream.status_code, headers=response_headers,
        )

    return app


async def supervise_tunnel(remote: Remote, socket: Path):
    while True:
        socket.unlink(missing_ok=True)
        tunnel = await asyncio.create_subprocess_exec(*remote.tunnel(local_socket=socket))
        try:
            await tunnel.wait()
        finally:
            if tunnel.returncode is None:
                with suppress(ProcessLookupError):
                    tunnel.terminate()
                await tunnel.wait()
        await asyncio.sleep(2)


async def supervise_ranking(config: Path):
    while True:
        worker = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "beings_leads.desktop_ranking.worker", str(config),
            stdin=asyncio.subprocess.PIPE,
        )
        try:
            await worker.wait()
        finally:
            if worker.returncode is None:
                worker.stdin.close()
                with suppress(ProcessLookupError):
                    worker.terminate()
                try:
                    await asyncio.wait_for(worker.wait(), timeout=3)
                except TimeoutError:
                    with suppress(ProcessLookupError):
                        worker.kill()
                    await worker.wait()
        await asyncio.sleep(2)


async def serve(dashboard: Path, remote: Remote, proof: str, *, config: Path):
    with TemporaryDirectory(prefix="beings-ssh-", dir="/tmp") as directory:
        socket = Path(directory) / "api.sock"
        async with httpx.AsyncClient(
            base_url=UPSTREAM, transport=httpx.AsyncHTTPTransport(uds=str(socket)),
            timeout=httpx.Timeout(600, connect=3), trust_env=False,
        ) as client:
            app = create_gateway(dashboard, remote.local_port, proof, client)
            server = uvicorn.Server(uvicorn.Config(
                app, host="127.0.0.1", port=remote.local_port, proxy_headers=False,
                access_log=False, timeout_graceful_shutdown=2,
            ))
            workers = (asyncio.create_task(supervise_tunnel(remote, socket)),
                       asyncio.create_task(supervise_ranking(config)))
            try:
                await server.serve()
            finally:
                for worker in workers:
                    worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dashboard", type=Path)
    parser.add_argument("config", type=Path)
    args = parser.parse_args()
    asyncio.run(serve(args.dashboard, read_config(args.config), os.environ["BEINGS_GATEWAY_PROOF"],
                      config=args.config))


if __name__ == "__main__":
    main()
