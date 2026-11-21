"""Pure in-memory thread-safe content store (zero local disk storage)."""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Dict, Optional


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


class ContentStore:
    """Stateless/in-memory store for prompts and TODO lists.

    Writes nothing to local disk. Admin updates live in process memory.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._global_prompt: Optional[dict] = None
        self._repo_prompts: Dict[str, dict] = {}
        self._repo_todos: Dict[str, dict] = {}

    def get_global_prompt(self) -> dict:
        with self._lock:
            if self._global_prompt is not None:
                return dict(self._global_prompt)
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
        with self._lock:
            self._global_prompt = entry
        return dict(entry)

    def get_repo_prompt(self, owner: str, repo: str) -> dict:
        key = _repo_key(owner, repo)
        with self._lock:
            repo_entry = self._repo_prompts.get(key)
            if repo_entry is not None:
                return {**repo_entry, "scope": "repo"}
            if self._global_prompt is not None:
                return {**self._global_prompt, "scope": "global"}
            return {
                "content": DEFAULT_GLOBAL_PROMPT,
                "updated_at": None,
                "is_default": True,
                "scope": "builtin_default",
            }

    def set_repo_prompt(self, owner: str, repo: str, content: str) -> dict:
        key = _repo_key(owner, repo)
        entry = {
            "content": content,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "is_default": False,
            "scope": "repo",
        }
        with self._lock:
            self._repo_prompts[key] = entry
        return dict(entry)

    def delete_repo_prompt(self, owner: str, repo: str) -> bool:
        key = _repo_key(owner, repo)
        with self._lock:
            return self._repo_prompts.pop(key, None) is not None

    def get_todo(self, owner: str, repo: str) -> dict:
        key = _repo_key(owner, repo)
        with self._lock:
            entry = self._repo_todos.get(key)
            if entry is not None:
                return dict(entry)
            return {
                "content": DEFAULT_TODO_TEMPLATE,
                "updated_at": None,
                "is_default": True,
            }

    def set_todo(self, owner: str, repo: str, content: str) -> dict:
        key = _repo_key(owner, repo)
        entry = {
            "content": content,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "is_default": False,
        }
        with self._lock:
            self._repo_todos[key] = entry
        return dict(entry)
