"""File-backed atomic persistence with thread + cross-process (flock) locking."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

try:
    import fcntl
except ImportError:  # Windows fallback
    fcntl = None  # type: ignore[assignment]


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


def _safe_repo_key(owner: str, repo: str) -> str:
    return f"{owner}__{repo}".replace("/", "_")


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


class ContentStore:
    """Thread-safe and multi-process-safe file store for prompts and TODO lists."""

    def __init__(self, data_dir: Path) -> None:
        self.base_dir = data_dir / "content"
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._lockfile = self.base_dir / ".store.lock"

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        with self._lock:
            if fcntl is None:
                yield
                return
            self._lockfile.parent.mkdir(parents=True, exist_ok=True)
            with open(self._lockfile, "a+", encoding="utf-8") as lf:
                fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

    def _global_prompt_path(self) -> Path:
        return self.base_dir / "global_prompt.json"

    def _repo_prompt_path(self, owner: str, repo: str) -> Path:
        return self.base_dir / "prompts" / f"{_safe_repo_key(owner, repo)}.json"

    def _repo_todo_path(self, owner: str, repo: str) -> Path:
        return self.base_dir / "todos" / f"{_safe_repo_key(owner, repo)}.json"

    def _read_entry(self, path: Path) -> Optional[dict]:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def get_global_prompt(self) -> dict:
        with self._locked():
            entry = self._read_entry(self._global_prompt_path())
            if entry is not None:
                return entry
            return {
                "content": DEFAULT_GLOBAL_PROMPT,
                "updated_at": None,
                "is_default": True,
            }

    def set_global_prompt(self, content: str) -> dict:
        entry = {
            "content": content,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "is_default": False,
        }
        with self._locked():
            _atomic_write_json(self._global_prompt_path(), entry)
        return entry

    def get_repo_prompt(self, owner: str, repo: str) -> dict:
        with self._locked():
            repo_entry = self._read_entry(self._repo_prompt_path(owner, repo))
            if repo_entry is not None:
                return {**repo_entry, "scope": "repo"}
            global_entry = self._read_entry(self._global_prompt_path())
            if global_entry is not None:
                return {**global_entry, "scope": "global"}
            return {
                "content": DEFAULT_GLOBAL_PROMPT,
                "updated_at": None,
                "is_default": True,
                "scope": "builtin_default",
            }

    def set_repo_prompt(self, owner: str, repo: str, content: str) -> dict:
        entry = {
            "content": content,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "is_default": False,
            "scope": "repo",
        }
        with self._locked():
            _atomic_write_json(self._repo_prompt_path(owner, repo), entry)
        return entry

    def delete_repo_prompt(self, owner: str, repo: str) -> bool:
        with self._locked():
            path = self._repo_prompt_path(owner, repo)
            if path.exists():
                path.unlink()
                return True
            return False

    def get_todo(self, owner: str, repo: str) -> dict:
        with self._locked():
            entry = self._read_entry(self._repo_todo_path(owner, repo))
            if entry is not None:
                return entry
            return {
                "content": DEFAULT_TODO_TEMPLATE,
                "updated_at": None,
                "is_default": True,
            }

    def set_todo(self, owner: str, repo: str, content: str) -> dict:
        entry = {
            "content": content,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "is_default": False,
        }
        with self._locked():
            _atomic_write_json(self._repo_todo_path(owner, repo), entry)
        return entry
