"""Admin-managed prompt & TODO content store (file-persisted, memory fallback).

Admin content is *configuration*, not transient request data, so it must survive
restarts. Two modes are supported:

* ``file``   -- ``content_dir`` is writable; mutations are persisted to a single
  ``content.json`` with atomic replace, guarded by a process lock plus a
  cross-process ``flock``.
* ``memory`` -- no ``content_dir`` or the directory is not writable (e.g. a
  ``read_only`` container without a mounted volume). Content is process-local and
  lost on restart; the mode and its reason are surfaced by ``/health`` and the
  startup banner so the degradation is never silent.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterator, Optional, Tuple

try:  # POSIX only; Windows falls back to the in-process lock
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger("git_gateway.store")

STATE_VERSION = 1


DEFAULT_GLOBAL_PROMPT = """# AI Agent Collaboration Guide — `{{OWNER}}/{{REPO}}`

You are collaborating on **{{OWNER}}/{{REPO}}** through a zero-permission Git patch gateway (`git-gateway`).
You do **NOT** need a GitHub account, SSH key, or Personal Access Token.

## Step 1 — Ask the User for Their Nickname (MANDATORY)
Before doing any code changes or submissions, ask the user to choose a short **nickname**
(letters, digits, `-`, `_`, 1–32 chars, e.g. `alice` or `dev-bob`).
All of your submitted changes will be pushed to the isolated branch:
`<nickname>/<original_branch>` (for example, `alice/main`), and a CI/CD Pull Request will be opened against `<original_branch>`.

## Step 2 — Read the TODO List
Fetch the latest task list from the TODO endpoint:
- **TODO Endpoint**: `GET {{TODO_URL}}`

Pick an open task (or follow the user's specific instruction).

## Step 3 — Clone the Repository (with Proxy Fallback)
1. First, try cloning directly from GitHub:
   ```bash
   git clone {{GITHUB_URL}}
   ```
2. If GitHub is unreachable, slow, or private, clone via this gateway's **read-only clone proxy** (zero credentials needed):
   ```bash
   git clone {{CLONE_PROXY_URL}}
   ```
> Note: Direct `git push` to the proxy is disabled (403). All contributions MUST be submitted via the Patch API below.

## Step 4 — Make Changes & Generate a Patch
Checkout the target base branch (e.g. `{{DEFAULT_BASE_BRANCH}}`), make your edits, and verify locally.
Record the exact **base branch name** (`base_branch`) and **base commit SHA** (`git rev-parse HEAD` before your edits).

Generate a patch using either:
- **Option A (Unified diff from working tree)**:
  ```bash
  git diff > changes.patch
  ```
- **Option B (Format-patch with embedded base commit — recommended for multi-commit work)**:
  ```bash
  git format-patch --stdout --base=<base_commit_sha> <base_branch>..HEAD > changes.patch
  ```

## Step 5 — Submit Patch (Auto-Push to `<nickname>/<original_branch>` & Open CI/CD PR)
Submit your patch via HTTP POST:

```bash
curl -sS -X POST "{{PATCH_URL}}?nickname=<NICKNAME>&base_branch={{DEFAULT_BASE_BRANCH}}&base_commit=<BASE_SHA>&message=feat:+describe+your+change" \\
  --data-binary @changes.patch
```

Or send JSON (`Content-Type: application/json`) with keys:
`nickname`, `base_branch`, `base_commit` (optional), `message`, `patch`, `pr_title` (optional), `pr_body` (optional).

The server will:
1. Validate your patch (protected files like `.github/workflows/*` cannot be modified).
2. Apply the patch and push to `<nickname>/<base_branch>`.
3. Automatically create (or update) an open CI/CD PR into `<base_branch>`.
4. Return `pr_number`, `ci_status_url`, and `ci_logs_url`.

## Step 6 — Check CI/CD Status & Read Error Logs
After submitting, poll the CI/CD status of your PR:
```bash
curl -sS "{{PRS_URL}}/<PR_NUMBER>/ci"
```
- Wait until `overall_status` is `"passed"` or `"failed"`.
- If `overall_status` is `"failed"`, fetch the actual CI/CD error logs directly:
  ```bash
  curl -sS "{{PRS_URL}}/<PR_NUMBER>/logs"
  ```
- Inspect the error log, fix the issue locally, regenerate the patch, and resubmit to the same `nickname` and `base_branch`.
  The gateway will push a new commit to `<nickname>/<base_branch>` and automatically re-trigger CI on the same PR!
"""


DEFAULT_TODO_TEMPLATE = """# TODO List — `{{OWNER}}/{{REPO}}`

*(Admin has not configured a TODO list for this repository yet. Ask the maintainer to update it via `PUT /admin/{{REPO_PATH}}/todo`.)*
"""




def _repo_key(owner: str, repo: str) -> str:
    return f"{owner}/{repo}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty_state() -> Dict[str, object]:
    return {
        "version": STATE_VERSION,
        "global_prompt": None,
        "repo_prompts": {},
        "repo_todos": {},
    }


class ContentStore:
    """Prompt & TODO store with optional disk persistence (see module docstring)."""

    def __init__(self, content_dir: Optional[Path] = None) -> None:
        self._lock = threading.RLock()
        self._dir = Path(content_dir) if content_dir is not None else None
        self._state_path = (self._dir / "content.json") if self._dir is not None else None
        self._lock_path = (self._dir / ".content.lock") if self._dir is not None else None
        self._persistent = False
        self._degraded_reason: Optional[str] = None
        self._last_error: Optional[str] = None
        self._state: Dict[str, object] = _empty_state()
        self._sig: Optional[Tuple[int, int]] = None

        if self._dir is not None:
            self._init_persistence()
        if not self._persistent:
            logger.warning(
                "content persistence DISABLED (%s) -- Admin prompts/TODOs will be lost on restart; "
                "mount a writable volume and set CONTENT_DIR to persist them",
                self._degraded_reason or "CONTENT_DIR not configured",
            )

    # ---------------------------------------------------------------- persistence
    @property
    def is_persistent(self) -> bool:
        return self._persistent

    def _init_persistence(self) -> None:
        assert self._dir is not None
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            probe = self._dir / ".write-probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            self._degraded_reason = f"{self._dir} is not writable ({type(exc).__name__}: {exc})"
            return

        self._persistent = True
        try:
            self._reload(force=True)
        except (OSError, ValueError) as exc:
            # A corrupt store must not prevent the gateway from starting.
            self._degraded_reason = f"existing content.json could not be loaded: {exc}"
            self._quarantine()
            logger.error("content.json unreadable (%s); preserved a copy and started empty", exc)

    def _quarantine(self) -> None:
        if self._state_path is None or not self._state_path.exists():
            return
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = self._state_path.with_name(f"content.corrupt-{stamp}.json")
        with contextlib.suppress(OSError):
            os.replace(self._state_path, target)
            logger.error("corrupt content store preserved at %s", target)
        self._state = _empty_state()
        self._sig = None

    def _signature(self) -> Optional[Tuple[int, int]]:
        if self._state_path is None:
            return None
        try:
            st = self._state_path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    @contextlib.contextmanager
    def _exclusive(self) -> Iterator[None]:
        """Process lock plus cross-process flock around read-modify-write."""
        with self._lock:
            if not self._persistent or self._lock_path is None or fcntl is None:
                yield
                return
            self._lock_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._lock_path, "a+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _reload(self, *, force: bool = False) -> None:
        if not self._persistent:
            return
        sig = self._signature()
        if not force and sig == self._sig:
            return
        self._sig = sig
        if sig is None or self._state_path is None:
            self._state = _empty_state()
            return

        raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("content store root must be a JSON object")

        state = _empty_state()
        global_prompt = raw.get("global_prompt")
        if isinstance(global_prompt, dict) and isinstance(global_prompt.get("content"), str):
            state["global_prompt"] = global_prompt
        for key in ("repo_prompts", "repo_todos"):
            bucket = raw.get(key)
            if isinstance(bucket, dict):
                state[key] = {
                    k: v for k, v in bucket.items()
                    if isinstance(v, dict) and isinstance(v.get("content"), str)
                }
        self._state = state

    def _write_atomic(self) -> None:
        if not self._persistent or self._dir is None or self._state_path is None:
            return
        fd, tmp_name = tempfile.mkstemp(prefix=".content-", suffix=".json", dir=str(self._dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._state, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self._state_path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
        self._sig = self._signature()

    def _mutate(self, apply: Callable[[Dict[str, object]], None]) -> None:
        with self._exclusive():
            if self._persistent:
                # Re-read under the lock so concurrent writers do not clobber each other.
                self._reload(force=True)
            apply(self._state)
            try:
                self._write_atomic()
            except OSError as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                logger.error("failed to persist content store: %s", self._last_error)
                raise

    def _snapshot(self) -> Dict[str, object]:
        with self._lock:
            if self._persistent:
                try:
                    self._reload()
                except (OSError, ValueError) as exc:
                    logger.warning("failed to reload content store: %s", exc)
            return self._state

    def info(self) -> dict:
        """Persistence status, surfaced by ``/health`` and the startup banner."""
        state = self._snapshot()
        repo_prompts = state.get("repo_prompts")
        repo_todos = state.get("repo_todos")
        return {
            "mode": "file" if self._persistent else "memory",
            "path": str(self._state_path) if self._persistent else None,
            "degraded_reason": self._degraded_reason,
            "last_error": self._last_error,
            "global_prompt_customized": isinstance(state.get("global_prompt"), dict),
            "repo_prompt_count": len(repo_prompts) if isinstance(repo_prompts, dict) else 0,
            "repo_todo_count": len(repo_todos) if isinstance(repo_todos, dict) else 0,
        }

    # ------------------------------------------------------------------- getters
    def get_global_prompt(self) -> dict:
        entry = self._snapshot().get("global_prompt")
        if isinstance(entry, dict):
            return dict(entry)
        return {
            "content": DEFAULT_GLOBAL_PROMPT,
            "updated_at": None,
            "is_default": True,
        }

    def get_repo_prompt(self, owner: str, repo: str) -> dict:
        state = self._snapshot()
        key = _repo_key(owner, repo)
        bucket = state.get("repo_prompts")
        repo_entry = bucket.get(key) if isinstance(bucket, dict) else None
        if isinstance(repo_entry, dict):
            return {**repo_entry, "scope": "repo"}
        global_entry = state.get("global_prompt")
        if isinstance(global_entry, dict):
            return {**global_entry, "scope": "global"}
        return {
            "content": DEFAULT_GLOBAL_PROMPT,
            "updated_at": None,
            "is_default": True,
            "scope": "builtin_default",
        }

    def get_todo(self, owner: str, repo: str) -> dict:
        state = self._snapshot()
        key = _repo_key(owner, repo)
        bucket = state.get("repo_todos")
        entry = bucket.get(key) if isinstance(bucket, dict) else None
        if isinstance(entry, dict):
            return dict(entry)
        return {
            "content": DEFAULT_TODO_TEMPLATE,
            "updated_at": None,
            "is_default": True,
        }

    # ------------------------------------------------------------------- setters
    def set_global_prompt(self, content: str) -> dict:
        entry = {"content": content, "updated_at": _now(), "is_default": False}

        def apply(state: Dict[str, object]) -> None:
            state["global_prompt"] = dict(entry)

        self._mutate(apply)
        return dict(entry)

    def set_repo_prompt(self, owner: str, repo: str, content: str) -> dict:
        key = _repo_key(owner, repo)
        entry = {
            "content": content,
            "updated_at": _now(),
            "is_default": False,
            "scope": "repo",
        }

        def apply(state: Dict[str, object]) -> None:
            bucket = state.setdefault("repo_prompts", {})
            if isinstance(bucket, dict):
                bucket[key] = dict(entry)

        self._mutate(apply)
        return dict(entry)

    def delete_repo_prompt(self, owner: str, repo: str) -> bool:
        key = _repo_key(owner, repo)
        result = {"removed": False}

        def apply(state: Dict[str, object]) -> None:
            bucket = state.get("repo_prompts")
            if isinstance(bucket, dict) and key in bucket:
                bucket.pop(key, None)
                result["removed"] = True

        self._mutate(apply)
        return result["removed"]

    def set_todo(self, owner: str, repo: str, content: str) -> dict:
        key = _repo_key(owner, repo)
        entry = {"content": content, "updated_at": _now(), "is_default": False}

        def apply(state: Dict[str, object]) -> None:
            bucket = state.setdefault("repo_todos", {})
            if isinstance(bucket, dict):
                bucket[key] = dict(entry)

        self._mutate(apply)
        return dict(entry)

