"""Read-only Git Smart-HTTP clone/fetch proxy (strictly blocks git-receive-pack)."""

from __future__ import annotations

import base64
from typing import AsyncIterator

import httpx
from fastapi import Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .config import Settings


HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
    "authorization",
}


def _upstream_auth_header(settings: Settings) -> str:
    tok = settings.resolve_github_token()
    if not tok:
        return ""
    encoded = base64.b64encode(f"x-access-token:{tok}".encode("utf-8")).decode("ascii")
    return f"Basic {encoded}"


async def _read_bounded_body(request: Request, max_bytes: int) -> bytes | None:
    """Read request body up to max_bytes; returns None if limit is exceeded."""
    cl_header = request.headers.get("content-length")
    if cl_header:
        try:
            if int(cl_header) > max_bytes:
                return None
        except ValueError:
            pass
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


async def handle_git_smart_http(
    request: Request,
    settings: Settings,
    owner: str,
    repo: str,
    repo_path: str,
    service_path: str,
) -> Response:
    """Proxy read-only `git clone` / `git fetch` requests to GitHub.

    Security guardrails:
      - `git-receive-pack` is always rejected with 403.
      - `info/refs` requires GET and exact whitelist `service=git-upload-pack` (prevents HPP).
      - `git-upload-pack` requires POST with bounded body size (prevents OOM DoS).
    """
    services = request.query_params.getlist("service")
    if service_path == "git-receive-pack" or any(s == "git-receive-pack" for s in services):
        return JSONResponse(
            status_code=403,
            content={
                "ok": False,
                "error": "direct git push is disabled on this read-only clone proxy",
                "hint": f"Submit your changes as a patch via POST /{repo_path}/patches?nickname=<your_nick>&base_branch=<branch>",
            },
        )

    if service_path == "info/refs":
        if request.method != "GET":
            return JSONResponse(status_code=405, content={"ok": False, "error": "info/refs only allows GET"})
        # Strict whitelist against HTTP Parameter Pollution (?service=git-receive-pack&service=git-upload-pack)
        if services != ["git-upload-pack"]:
            return JSONResponse(
                status_code=403,
                content={
                    "ok": False,
                    "error": "only service=git-upload-pack is allowed on this read-only clone proxy",
                },
            )
        upstream_url = f"{settings.github_web_base}/{owner}/{repo}.git/info/refs?service=git-upload-pack"
    elif service_path == "git-upload-pack":
        if request.method != "POST":
            return JSONResponse(status_code=405, content={"ok": False, "error": "git-upload-pack only allows POST"})
        upstream_url = f"{settings.github_web_base}/{owner}/{repo}.git/git-upload-pack"
    else:
        return JSONResponse(status_code=404, content={"ok": False, "error": "unsupported git endpoint"})

    fwd_headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in HOP_BY_HOP_HEADERS
    }
    fwd_headers["User-Agent"] = fwd_headers.get("User-Agent", "git/2.39.0")
    auth = _upstream_auth_header(settings)
    if auth:
        fwd_headers["Authorization"] = auth

    body: bytes | None = None
    if request.method == "POST":
        body = await _read_bounded_body(request, settings.max_git_proxy_body_bytes)
        if body is None:
            return JSONResponse(
                status_code=413,
                content={
                    "ok": False,
                    "error": f"git-upload-pack request body exceeds limit ({settings.max_git_proxy_body_bytes} bytes)",
                },
            )

    client = httpx.AsyncClient(
        http2=False,
        follow_redirects=True,
        timeout=httpx.Timeout(connect=30.0, read=600.0, write=120.0, pool=30.0),
    )
    req = client.build_request(
        method=request.method,
        url=upstream_url,
        headers=fwd_headers,
        content=body,
    )
    try:
        upstream_resp = await client.send(req, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        return JSONResponse(
            status_code=502,
            content={"ok": False, "error": f"upstream git clone proxy error: {exc}"},
        )

    # Preserve content-encoding because we stream raw bytes via aiter_raw()
    resp_headers = {
        k: v
        for k, v in upstream_resp.headers.items()
        if k.lower() not in HOP_BY_HOP_HEADERS
    }

    async def stream_body() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream_resp.aiter_raw():
                yield chunk
        finally:
            await upstream_resp.aclose()
            await client.aclose()

    return StreamingResponse(
        stream_body(),
        status_code=upstream_resp.status_code,
        headers=resp_headers,
        media_type=upstream_resp.headers.get("content-type"),
    )
