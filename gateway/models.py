"""Pydantic models and strict validation for nicknames, branches, patches, and CI responses."""

from __future__ import annotations

import re
from typing import Any, List, Optional

from pydantic import BaseModel, Field, field_validator


NICKNAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,31}$")
RESERVED_NICKNAMES = {
    "main",
    "master",
    "head",
    "refs",
    "origin",
    "admin",
    "system",
    "release",
    "tags",
    "heads",
}
COMMIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
BRANCH_BAD_RE = re.compile(r"(^/|/$|//|\.\.|@\{|[\x00-\x20\x7f~^:?*\[\\])")


def validate_nickname(value: str) -> str:
    nick = value.strip()
    if not nick:
        raise ValueError("nickname is required (ask the user to set a nickname first)")
    if not NICKNAME_RE.match(nick):
        raise ValueError(
            "nickname must be 1-32 chars, start with alphanumeric, and contain only [a-zA-Z0-9_-]"
        )
    if nick.lower() in RESERVED_NICKNAMES:
        raise ValueError(f"nickname '{nick}' is reserved; choose a personal handle")
    return nick


def validate_branch_name(value: str) -> str:
    branch = value.strip()
    if not branch:
        raise ValueError("base_branch is required (e.g. 'main' or 'feat/login')")
    if branch.startswith("refs/"):
        branch = branch.removeprefix("refs/heads/")
    if (
        BRANCH_BAD_RE.search(branch)
        or branch.endswith(".lock")
        or branch.endswith(".")
        or branch in {"@", "HEAD"}
    ):
        raise ValueError(f"invalid git branch name: '{value}'")
    if len(branch) > 128:
        raise ValueError("branch name too long (max 128 chars)")
    return branch


def build_agent_branch(nickname: str, original_branch: str) -> str:
    """Construct the isolated target branch `<nickname>/<original_branch>`."""
    nick = validate_nickname(nickname)
    orig = validate_branch_name(original_branch)
    # Prevent double-prefixing if the agent accidentally passed <nickname>/<orig>
    prefix = f"{nick}/"
    if orig.startswith(prefix):
        orig = orig[len(prefix):]
        orig = validate_branch_name(orig)
    return f"{nick}/{orig}"


class PatchSubmission(BaseModel):
    nickname: str = Field(..., description="Contributor nickname (push target becomes <nickname>/<base_branch>)")
    base_branch: str = Field(..., description="Original target branch this patch is based on (e.g. main)")
    base_commit: Optional[str] = Field(
        default=None,
        description="Optional base commit SHA; if omitted, auto-detected from format-patch or resolved from base_branch",
    )
    message: str = Field(..., min_length=1, max_length=2000, description="Commit message")
    patch: str = Field(..., min_length=1, description="Unified diff (`git diff`) or `git format-patch` content")
    pr_title: Optional[str] = Field(default=None, max_length=256, description="Optional PR title override")
    pr_body: Optional[str] = Field(default=None, max_length=16000, description="Optional PR description")
    create_pr: bool = Field(default=True, description="Automatically create or reuse an open CI/CD PR")

    @field_validator("nickname")
    @classmethod
    def _check_nickname(cls, v: str) -> str:
        return validate_nickname(v)

    @field_validator("base_branch")
    @classmethod
    def _check_branch(cls, v: str) -> str:
        return validate_branch_name(v)

    @field_validator("base_commit")
    @classmethod
    def _check_commit(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not v.strip():
            return None
        v = v.strip()
        if not COMMIT_SHA_RE.match(v):
            raise ValueError("base_commit must be a 7-40 char hex commit SHA")
        return v


class PatchResponse(BaseModel):
    ok: bool
    status: str  # pushed | noop | failed
    repo: str
    nickname: str
    base_branch: str
    target_branch: str
    base_commit: Optional[str] = None
    commit_sha: Optional[str] = None
    commit_short: Optional[str] = None
    message: Optional[str] = None
    pr_number: Optional[int] = None
    pr_url: Optional[str] = None
    pr_reused: bool = False
    ci_status_url: Optional[str] = None
    ci_logs_url: Optional[str] = None
    error: Optional[str] = None
    detail: Optional[str] = None
    elapsed_ms: int = 0


class AdminContentUpdate(BaseModel):
    content: str = Field(..., description="Markdown text content")


class CIJobSummary(BaseModel):
    job_id: int
    name: str
    status: str
    conclusion: Optional[str] = None
    html_url: Optional[str] = None
    failed_steps: List[str] = Field(default_factory=list)
    logs_url: Optional[str] = None


class PRCIStatusResponse(BaseModel):
    ok: bool = True
    repo: str
    pr_number: int
    title: str
    state: str
    head_branch: str
    base_branch: str
    head_sha: str
    pr_url: str
    mergeable: Optional[bool] = None
    overall_status: str  # passed | failed | running | pending | none
    passed_count: int = 0
    failed_count: int = 0
    pending_count: int = 0
    check_runs: List[dict[str, Any]] = Field(default_factory=list)
    failed_jobs: List[CIJobSummary] = Field(default_factory=list)
    logs_url: str
    hint: str
