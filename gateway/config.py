"""Configuration and repository mapping/whitelist resolution."""

from __future__ import annotations

import fnmatch
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


DEFAULT_BLOCKED_PATHS = [
    ".github/workflows/*",
    ".github/workflows/**",
    ".github/actions/*",
    ".github/actions/**",
]


@dataclass
class Settings:
    host: str = "0.0.0.0"
    port: int = 8000
    public_base_url: str = ""  # e.g. https://proxy.com (auto-inferred from request if empty)
    data_dir: Path = field(default_factory=lambda: Path("./data"))
    github_token: str = ""  # PAT with repo/actions:read; optional fallback to `gh auth token`
    admin_token: str = ""  # Required for PUT/DELETE /admin/* endpoints
    github_web_base: str = "https://github.com"
    github_api_base: str = "https://api.github.com"
    allowed_repos: List[str] = field(default_factory=lambda: ["Hana-ame/*"])
    # Optional path aliases: {"path/to/repo": "Hana-ame/repo"}
    repo_aliases: Dict[str, str] = field(default_factory=dict)
    default_base_branch: str = "main"
    blocked_patch_paths: List[str] = field(default_factory=lambda: list(DEFAULT_BLOCKED_PATHS))
    max_patch_bytes: int = 10 * 1024 * 1024  # 10 MiB
    git_timeout_seconds: int = 120
    allow_gh_cli_fallback: bool = True

    def resolve_github_token(self) -> str:
        """Return configured GITHUB_TOKEN or fall back to `gh auth token` if enabled."""
        if self.github_token:
            return self.github_token
        if not self.allow_gh_cli_fallback:
            return ""
        try:
            proc = subprocess.run(
                ["gh", "auth", "token"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return proc.stdout.strip()
        except FileNotFoundError:
            pass
        except subprocess.SubprocessError:
            pass
        return ""

    def is_repo_allowed(self, owner: str, repo: str) -> bool:
        full = f"{owner}/{repo}"
        for pattern in self.allowed_repos:
            if fnmatch.fnmatchcase(full, pattern):
                return True
        return False

    def is_patch_path_blocked(self, rel_path: str) -> bool:
        normalized = rel_path.replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        normalized = normalized.lstrip("/")
        for pat in self.blocked_patch_paths:
            if fnmatch.fnmatchcase(normalized, pat):
                return True
            if pat.endswith("/**") and normalized.startswith(pat[:-3] + "/"):
                return True
        return False

    def resolve_repo_path(self, raw_path: str) -> Optional[Tuple[str, str, str]]:
        """Resolve a URL prefix (alias or owner/repo) to (public_prefix, owner, repo).

        Supports both:
          - Aliases like `path/to/repo` -> `Hana-ame/repo`
          - Standard `<owner>/<repo>` (with optional `.git` suffix stripped)
        """
        cleaned = raw_path.strip("/")
        if not cleaned:
            return None

        # 1. Exact alias match (with or without .git suffix)
        cleaned_no_git = cleaned[:-4] if cleaned.endswith(".git") else cleaned
        if cleaned_no_git in self.repo_aliases:
            target = self.repo_aliases[cleaned_no_git].strip("/")
            parts = target.split("/")
            if len(parts) == 2 and parts[0] and parts[1]:
                return cleaned_no_git, parts[0], parts[1]

        # 2. Direct <owner>/<repo>
        parts = cleaned.split("/")
        if len(parts) == 2 and parts[0] and parts[1]:
            owner = parts[0]
            repo = parts[1][:-4] if parts[1].endswith(".git") else parts[1]
            if owner and repo:
                return f"{owner}/{repo}", owner, repo

        return None

    def split_repo_and_suffix(self, full_path: str) -> Optional[Tuple[str, str, str, str]]:
        """Split a request path into (public_prefix, owner, repo, suffix).

        Matches longest configured alias first; otherwise treats the first two
        segments as `<owner>/<repo>`.
        """
        cleaned = full_path.strip("/")
        if not cleaned:
            return None

        # Check configured aliases (longest prefix first)
        for alias in sorted(self.repo_aliases.keys(), key=len, reverse=True):
            alias_clean = alias.strip("/")
            for candidate in (alias_clean, f"{alias_clean}.git"):
                if cleaned == candidate or cleaned.startswith(candidate + "/"):
                    suffix = cleaned[len(candidate):].lstrip("/")
                    target = self.repo_aliases[alias].strip("/")
                    tparts = target.split("/")
                    if len(tparts) == 2:
                        return alias_clean, tparts[0], tparts[1], suffix

        # Direct <owner>/<repo>[.git][/<suffix>]
        parts = cleaned.split("/", 2)
        if len(parts) < 2 or not parts[0] or not parts[1]:
            return None
        owner = parts[0]
        raw_repo = parts[1]
        repo = raw_repo[:-4] if raw_repo.endswith(".git") else raw_repo
        if not repo:
            return None
        suffix = parts[2] if len(parts) == 3 else ""
        return f"{owner}/{repo}", owner, repo, suffix


def parse_aliases(raw: str) -> Dict[str, str]:
    """Parse comma-separated `alias=owner/repo` entries."""
    result: Dict[str, str] = {}
    if not raw:
        return result
    for item in raw.split(","):
        item = item.strip()
        if not item or "=" not in item:
            continue
        alias, target = item.split("=", 1)
        alias = alias.strip().strip("/")
        target = target.strip().strip("/")
        if alias and "/" in target:
            result[alias] = target
    return result


def load_settings_from_env() -> Settings:
    """Load Settings from environment variables."""
    allow_raw = os.getenv("ALLOWED_REPOS", "Hana-ame/*")
    allowed = [x.strip() for x in allow_raw.split(",") if x.strip()]

    blocked_raw = os.getenv("BLOCKED_PATCH_PATHS", "")
    blocked = (
        [x.strip() for x in blocked_raw.split(",") if x.strip()]
        if blocked_raw
        else list(DEFAULT_BLOCKED_PATHS)
    )
    if blocked_raw.strip().lower() == "none":
        blocked = []

    return Settings(
        host=os.getenv("GATEWAY_HOST", "0.0.0.0"),
        port=int(os.getenv("GATEWAY_PORT", "8000")),
        public_base_url=os.getenv("PUBLIC_BASE_URL", "").rstrip("/"),
        data_dir=Path(os.getenv("DATA_DIR", "./data")),
        github_token=os.getenv("GITHUB_TOKEN", "").strip(),
        admin_token=os.getenv("ADMIN_TOKEN", "").strip(),
        github_web_base=os.getenv("GITHUB_WEB_BASE", "https://github.com").rstrip("/"),
        github_api_base=os.getenv("GITHUB_API_BASE", "https://api.github.com").rstrip("/"),
        allowed_repos=allowed,
        repo_aliases=parse_aliases(os.getenv("REPO_ALIASES", "")),
        default_base_branch=os.getenv("DEFAULT_BASE_BRANCH", "main"),
        blocked_patch_paths=blocked,
        max_patch_bytes=int(os.getenv("MAX_PATCH_BYTES", str(10 * 1024 * 1024))),
        git_timeout_seconds=int(os.getenv("GIT_TIMEOUT_SECONDS", "120")),
    )
