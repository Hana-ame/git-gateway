"""FastAPI application and unified route dispatcher for the zero-permission git-gateway."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import secrets
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import ValidationError

from . import __version__
from .config import Settings
from .git_ops import GitPatchEngine, PatchValidationError
from .git_proxy import handle_git_smart_http
from .github_client import GitHubAPIError, GitHubClient
from .models import AdminContentUpdate, PatchResponse, PatchSubmission
from .prompt_renderer import render_agent_landing, render_todo_page
from .store import ContentStore


PR_CI_RE = re.compile(r"^prs/(\d+)/ci$")
PR_LOGS_RE = re.compile(r"^prs/(\d+)/logs$")
JOB_LOGS_RE = re.compile(r"^actions/jobs/(\d+)/logs$")


def _wants_json(request: Request) -> bool:
    fmt = request.query_params.get("format", "").lower()
    if fmt == "json":
        return True
    if fmt in ("md", "markdown", "text", "plain"):
        return False
    accept = request.headers.get("accept", "").lower()
    return "application/json" in accept and "text/markdown" not in accept and "text/plain" not in accept


def _infer_base_url(request: Request, settings: Settings) -> str:
    if settings.public_base_url:
        return settings.public_base_url
    if settings.trust_proxy_headers:
        proto = request.headers.get("x-forwarded-proto") or request.url.scheme
        host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    else:
        proto = request.url.scheme
        host = request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}".rstrip("/")


def _parse_int_param(
    request: Request,
    name: str,
    *,
    default: int,
    min_value: int,
    max_value: int,
) -> int:
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return default
    try:
        val = int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"query parameter '{name}' must be an integer")
    if val < min_value or val > max_value:
        raise HTTPException(
            status_code=400,
            detail=f"query parameter '{name}' must be between {min_value} and {max_value}",
        )
    return val


def _verify_admin(request: Request, settings: Settings) -> None:
    if not settings.admin_token:
        raise HTTPException(
            status_code=503,
            detail="ADMIN_TOKEN is not configured on this server; admin endpoints are disabled.",
        )
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not token:
        token = request.headers.get("x-admin-token", "").strip()
    if not token or not secrets.compare_digest(token, settings.admin_token):
        raise HTTPException(status_code=401, detail="invalid or missing admin token")


async def _read_content_body(request: Request) -> str:
    raw = await request.body()
    ctype = request.headers.get("content-type", "").lower()
    if "application/json" in ctype:
        try:
            payload = json.loads(raw.decode("utf-8"))
            parsed = AdminContentUpdate.model_validate(payload)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise HTTPException(status_code=400, detail=f"invalid JSON body: {exc}")
        return parsed.content
    return raw.decode("utf-8", errors="replace")


def _format_logs_as_text(data: dict[str, Any]) -> str:
    jobs = data.get("jobs") or []
    header = (
        f"# CI/CD Error Logs — {data.get('repo')} PR #{data.get('pr_number')} "
        f"(head {str(data.get('head_sha', ''))[:7]})\n"
    )
    if not jobs:
        return header + "\nNo failed CI/CD jobs found for this PR head commit.\n"

    sections = [header]
    for job in jobs:
        steps = ", ".join(job.get("failed_steps") or []) or "(none)"
        sections.append(
            f"\n=== Job #{job.get('job_id')}: {job.get('name')} "
            f"[{job.get('conclusion')}] (failed steps: {steps}) ===\n"
            f"{job.get('log', '')}\n"
        )
    return "\n".join(sections)


def create_app(settings: Settings) -> FastAPI:
    store = ContentStore()
    gh = GitHubClient(settings)
    git_engine = GitPatchEngine(settings)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Warm up token cache at startup so request handlers never block on subprocess
        settings.resolve_github_token()
        try:
            yield
        finally:
            await gh.aclose()

    app = FastAPI(
        title="git-gateway",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    app.state.settings = settings
    app.state.store = store
    app.state.github = gh
    app.state.git_engine = git_engine

    # ------------------------------------------------------------------ #
    # System Endpoints
    # ------------------------------------------------------------------ #

    @app.get("/")
    async def root(request: Request) -> JSONResponse:
        base = _infer_base_url(request, settings)
        return JSONResponse(
            {
                "service": "git-gateway",
                "version": __version__,
                "repo_rules_file": str(settings.repo_rules_file) if settings.repo_rules_file else None,
                "repo_rules_count": len(settings.active_rules()),
                "repo_aliases": settings.repo_aliases,
                "usage": {
                    "agent_instructions": f"GET {base}/<repo_path>",
                    "todo_list": f"GET {base}/<repo_path>/todo",
                    "clone_proxy_readonly": f"git clone {base}/<repo_path>.git",
                    "submit_patch": f"POST {base}/<repo_path>/patches",
                    "list_prs": f"GET {base}/<repo_path>/prs?nickname=<nick>",
                    "pr_ci_status": f"GET {base}/<repo_path>/prs/<pr_number>/ci",
                    "pr_ci_error_logs": f"GET {base}/<repo_path>/prs/<pr_number>/logs",
                },
                "admin_endpoints": {
                    "global_prompt": "GET/PUT /admin/prompt",
                    "repo_prompt": "GET/PUT/DELETE /admin/<repo_path>/prompt",
                    "repo_todo": "GET/PUT /admin/<repo_path>/todo",
                },
            }
        )

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse(
            {
                "ok": True,
                "version": __version__,
                "github_token_configured": bool(settings.resolve_github_token()),
                "github_token_source": settings.token_source(),
                "admin_token_configured": bool(settings.admin_token),
                "repo_rules_file": str(settings.repo_rules_file) if settings.repo_rules_file else None,
                "repo_rules_count": len(settings.active_rules()),
                "repo_aliases": settings.repo_aliases,
                "temp_dir": str(settings.temp_dir),
                "local_storage": "none (in-memory + OS temp dir only)",
            }
        )

    # ------------------------------------------------------------------ #
    # Admin Endpoints (Prompt & TODO Management)
    # ------------------------------------------------------------------ #

    @app.get("/admin/prompt")
    async def admin_get_global_prompt(request: Request) -> JSONResponse:
        _verify_admin(request, settings)
        return JSONResponse({"ok": True, **store.get_global_prompt()})

    @app.put("/admin/prompt")
    async def admin_put_global_prompt(request: Request) -> JSONResponse:
        _verify_admin(request, settings)
        content = await _read_content_body(request)
        if not content.strip():
            raise HTTPException(status_code=400, detail="prompt content cannot be empty")
        entry = store.set_global_prompt(content)
        return JSONResponse({"ok": True, **entry})

    @app.api_route("/admin/{subpath:path}", methods=["GET", "PUT", "DELETE"])
    async def admin_repo_content(request: Request, subpath: str) -> JSONResponse:
        _verify_admin(request, settings)
        cleaned = subpath.strip("/")
        if cleaned.endswith("/prompt"):
            raw_repo = cleaned[: -len("/prompt")]
            kind = "prompt"
        elif cleaned.endswith("/todo"):
            raw_repo = cleaned[: -len("/todo")]
            kind = "todo"
        else:
            raise HTTPException(
                status_code=404,
                detail="admin path must end with /prompt or /todo",
            )

        resolved = settings.resolve_repo_path(raw_repo)
        if not resolved:
            raise HTTPException(status_code=400, detail=f"invalid repo path: '{raw_repo}'")
        repo_path, owner, repo = resolved
        if not settings.is_repo_allowed(owner, repo):
            raise HTTPException(status_code=403, detail=f"repo not allowed: {owner}/{repo}")

        if kind == "prompt":
            if request.method == "GET":
                return JSONResponse(
                    {"ok": True, "repo": f"{owner}/{repo}", "repo_path": repo_path, **store.get_repo_prompt(owner, repo)}
                )
            if request.method == "DELETE":
                deleted = store.delete_repo_prompt(owner, repo)
                return JSONResponse(
                    {"ok": True, "repo": f"{owner}/{repo}", "deleted_override": deleted}
                )
            content = await _read_content_body(request)
            if not content.strip():
                raise HTTPException(status_code=400, detail="prompt content cannot be empty")
            entry = store.set_repo_prompt(owner, repo, content)
            return JSONResponse({"ok": True, "repo": f"{owner}/{repo}", "repo_path": repo_path, **entry})

        # kind == "todo"
        if request.method == "GET":
            return JSONResponse(
                {"ok": True, "repo": f"{owner}/{repo}", "repo_path": repo_path, **store.get_todo(owner, repo)}
            )
        if request.method == "DELETE":
            raise HTTPException(status_code=405, detail="use PUT with empty/new content to update TODO")
        content = await _read_content_body(request)
        entry = store.set_todo(owner, repo, content)
        return JSONResponse({"ok": True, "repo": f"{owner}/{repo}", "repo_path": repo_path, **entry})

    # ------------------------------------------------------------------ #
    # Unified Repository Dispatcher (Supports aliases + <owner>/<repo>)
    # ------------------------------------------------------------------ #

    @app.api_route("/{full_path:path}", methods=["GET", "POST"])
    async def repo_dispatch(request: Request, full_path: str) -> Response:
        split = settings.split_repo_and_suffix(full_path)
        if not split:
            return JSONResponse(status_code=404, content={"ok": False, "error": "not found"})

        repo_path, owner, repo, suffix = split
        if not settings.is_repo_allowed(owner, repo):
            return JSONResponse(
                status_code=403,
                content={"ok": False, "error": f"repo not allowed: {owner}/{repo}"},
            )

        base_url = _infer_base_url(request, settings)
        repo_base_url = f"{base_url}/{repo_path}"

        # 1. Read-only Git Smart-HTTP proxy (clone/fetch allowed, push strictly blocked)
        if suffix in ("info/refs", "git-upload-pack", "git-receive-pack"):
            return await handle_git_smart_http(request, settings, owner, repo, repo_path, suffix)

        # 2. Agent Landing Page: GET /<repo_path>
        if suffix == "" and request.method == "GET":
            data = render_agent_landing(settings, store, base_url, repo_path, owner, repo)
            if _wants_json(request):
                return JSONResponse({"ok": True, **data})
            return PlainTextResponse(data["markdown"], media_type="text/markdown; charset=utf-8")

        # 3. Public TODO List: GET /<repo_path>/todo
        if suffix == "todo" and request.method == "GET":
            data = render_todo_page(settings, store, base_url, repo_path, owner, repo)
            if _wants_json(request):
                return JSONResponse({"ok": True, **data})
            return PlainTextResponse(data["content"], media_type="text/markdown; charset=utf-8")

        # 4. Patch Submission: POST /<repo_path>/patches
        if suffix == "patches" and request.method == "POST":
            return await _handle_patch_submission(
                request, settings, git_engine, gh, owner, repo, repo_base_url
            )

        # 5. List PRs: GET /<repo_path>/prs
        if suffix == "prs" and request.method == "GET":
            state = request.query_params.get("state", "open")
            nickname = request.query_params.get("nickname") or None
            base_branch = request.query_params.get("base_branch") or None
            limit = _parse_int_param(request, "limit", default=30, min_value=1, max_value=100)
            try:
                prs = await gh.list_prs(
                    owner,
                    repo,
                    state=state,
                    nickname=nickname,
                    base_branch=base_branch,
                    limit=limit,
                )
            except GitHubAPIError as exc:
                return JSONResponse(status_code=502, content={"ok": False, "error": exc.message})
            return JSONResponse({"ok": True, "repo": f"{owner}/{repo}", "prs": prs})

        # 6. PR CI/CD Status: GET /<repo_path>/prs/<pr_number>/ci
        m = PR_CI_RE.match(suffix)
        if m and request.method == "GET":
            pr_number = int(m.group(1))
            try:
                status = await gh.get_pr_ci_status(
                    owner, repo, pr_number, repo_base_url=repo_base_url
                )
            except GitHubAPIError as exc:
                code = 404 if exc.status_code == 404 else 502
                return JSONResponse(status_code=code, content={"ok": False, "error": exc.message})
            return JSONResponse(status.model_dump())

        # 7. PR CI/CD Error Logs: GET /<repo_path>/prs/<pr_number>/logs
        m = PR_LOGS_RE.match(suffix)
        if m and request.method == "GET":
            pr_number = int(m.group(1))
            tail = _parse_int_param(request, "tail", default=200, min_value=0, max_value=10000)
            include_all = request.query_params.get("all", "").lower() in ("1", "true", "yes")
            try:
                data = await gh.get_pr_logs(
                    owner, repo, pr_number, tail=tail, include_all=include_all
                )
            except GitHubAPIError as exc:
                code = 404 if exc.status_code == 404 else 502
                return JSONResponse(status_code=code, content={"ok": False, "error": exc.message})
            if _wants_json(request):
                return JSONResponse(data)
            return PlainTextResponse(_format_logs_as_text(data), media_type="text/plain; charset=utf-8")

        # 8. Direct Job Error Logs: GET /<repo_path>/actions/jobs/<job_id>/logs
        m = JOB_LOGS_RE.match(suffix)
        if m and request.method == "GET":
            job_id = int(m.group(1))
            tail = _parse_int_param(request, "tail", default=200, min_value=0, max_value=10000)
            try:
                log_text = await gh.get_job_logs(owner, repo, job_id, tail=tail)
            except GitHubAPIError as exc:
                code = 404 if exc.status_code == 404 else 502
                return JSONResponse(status_code=code, content={"ok": False, "error": exc.message})
            if _wants_json(request):
                return JSONResponse({"ok": True, "repo": f"{owner}/{repo}", "job_id": job_id, "log": log_text})
            return PlainTextResponse(log_text, media_type="text/plain; charset=utf-8")

        return JSONResponse(status_code=404, content={"ok": False, "error": f"unknown endpoint: /{full_path}"})

    return app


async def _handle_patch_submission(
    request: Request,
    settings: Settings,
    git_engine: GitPatchEngine,
    gh: GitHubClient,
    owner: str,
    repo: str,
    repo_base_url: str,
) -> JSONResponse:
    raw_body = await request.body()
    if len(raw_body) > settings.max_patch_bytes:
        return JSONResponse(
            status_code=413,
            content={"ok": False, "error": f"patch exceeds limit ({settings.max_patch_bytes} bytes)"},
        )

    ctype = request.headers.get("content-type", "").lower()
    try:
        if "application/json" in ctype:
            payload = json.loads(raw_body.decode("utf-8"))
            sub = PatchSubmission.model_validate(payload)
        else:
            q = request.query_params
            create_pr_raw = q.get("create_pr", "true").lower()
            sub = PatchSubmission(
                nickname=q.get("nickname", ""),
                base_branch=q.get("base_branch", settings.default_base_branch),
                base_commit=q.get("base_commit") or None,
                message=q.get("message") or request.headers.get("x-commit-message", "") or "agent patch",
                patch=raw_body.decode("utf-8", errors="replace"),
                pr_title=q.get("pr_title") or None,
                pr_body=q.get("pr_body") or None,
                create_pr=create_pr_raw not in ("0", "false", "no"),
                patch_mode=q.get("patch_mode", "auto"),
            )
    except (json.JSONDecodeError, ValidationError) as exc:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "status": "failed", "error": "invalid submission", "detail": str(exc)},
        )

    # Apply patch & push to <nickname>/<base_branch> in worker thread
    try:
        result = await asyncio.to_thread(
            git_engine.apply_and_push,
            owner,
            repo,
            nickname=sub.nickname,
            base_branch=sub.base_branch,
            base_commit=sub.base_commit,
            message=sub.message,
            patch_text=sub.patch,
            patch_mode=sub.patch_mode,
        )
    except PatchValidationError as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "ok": False,
                "status": "failed",
                "repo": f"{owner}/{repo}",
                "nickname": sub.nickname,
                "base_branch": sub.base_branch,
                "error": exc.error,
                "detail": exc.detail,
            },
        )

    resp = PatchResponse(
        ok=True,
        status=result.status,
        repo=f"{owner}/{repo}",
        nickname=sub.nickname,
        base_branch=result.base_branch,
        target_branch=result.target_branch,
        base_commit=result.base_commit,
        commit_sha=result.commit_sha,
        commit_short=result.commit_short,
        message=sub.message,
        elapsed_ms=result.elapsed_ms,
    )

    if sub.create_pr:
        pr_title = sub.pr_title or f"[{sub.nickname}] {sub.message.splitlines()[0][:120]}"
        pr_body = (
            sub.pr_body
            or (
                f"Automated CI/CD PR submitted via git-gateway.\n\n"
                f"- **Contributor nickname**: `{sub.nickname}`\n"
                f"- **Target branch**: `{result.target_branch}` → `{result.base_branch}`\n"
                f"- **Base commit**: `{result.base_commit[:12]}`\n"
                f"- **Latest commit**: `{result.commit_short}`\n"
            )
        )
        try:
            pr_num, pr_url, reused = await gh.ensure_pr(
                owner,
                repo,
                head_branch=result.target_branch,
                base_branch=result.base_branch,
                title=pr_title,
                body=pr_body,
            )
            resp.pr_number = pr_num
            resp.pr_url = pr_url
            resp.pr_reused = reused
            resp.ci_status_url = f"{repo_base_url}/prs/{pr_num}/ci"
            resp.ci_logs_url = f"{repo_base_url}/prs/{pr_num}/logs"
        except GitHubAPIError as exc:
            resp.detail = f"pushed to {result.target_branch}, but PR creation failed: {exc.message}"

    return JSONResponse(status_code=200, content=resp.model_dump())
