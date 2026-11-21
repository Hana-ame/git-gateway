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


async def handle_git_smart_http(
    request: Request,
    settings: Settings,
    owner: str,
    repo: str,
    repo_path: str,
    service_path: str,
) -> Response:
    """Proxy read-only `git clone` / `git fetch` requests to GitHub.

    Any attempt to push (`git-receive-pack`) is rejected with 403 Forbidden.
    """
    query_service = request.query_params.get("service", "")
    if service_path == "git-receive-pack" or query_service == "git-receive-pack":
        return JSONResponse(
            status_code=403,
            content={
                "ok": False,
                "error": "direct git push is disabled on this read-only clone proxy",
                "hint": f"Submit your changes as a patch via POST /{repo_path}/patches?nickname=<your_nick>&base_branch=<branch>",
            },
        )

    if service_path not in ("info/refs", "git-upload-pack"):
        return JSONResponse(status_code=404, content={"ok": False, "error": "unsupported git endpoint"})

    upstream_url = f"{settings.github_web_base}/{owner}/{repo}.git/{service_path}"
    if request.url.query:
        upstream_url += f"?{request.url.query}"

    fwd_headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in HOP_BY_HOP_HEADERS
    }
    fwd_headers["User-Agent"] = fwd_headers.get("User-Agent", "git/2.39.0")
    auth = _upstream_auth_header(settings)
    if auth:
        fwd_headers["Authorization"] = auth

    body = await request.body() if request.method == "POST" else None

    # Disable HTTP/2 to avoid backend pack stream truncation on large clones
    client = httpx.AsyncClient(http2=False, timeout=httpx.Timeout(600.0, connect=30.0))
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

    resp_headers = {
        k: v
        for k, v in upstream_resp.headers.items()
        if k.lower() not in HOP_BY_HOP_HEADERS and k.lower() != "content-encoding"
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
