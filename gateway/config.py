"""Configuration, gitignore-style repo allow/disallow rules, and path mapping."""

from __future__ import annotations

import fnmatch
import os
import posixpath
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


DEFAULT_BLOCKED_PATHS = [
    ".github/workflows/*",
    ".github/workflows/**",
    ".github/actions/*",
    ".github/actions/**",
]


def default_temp_dir() -> Path:
    r"""Return OS temp directory (/tmp/git-gateway on POSIX, %TEMP%\git-gateway on Windows)."""
    return Path(tempfile.gettempdir()) / "git-gateway"


def _parse_bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class RepoRule:
    pattern: str
    negated: bool  # True if line starts with '!' -> disallow
    raw: str


class RepoRuleMatcher:
    """Gitignore-style repository allow/disallow matcher.

    Evaluation semantics (last match wins):
      - Default (no rule matched): disallow (False)
      - Line matching without '!': allow (True)
      - Line matching with leading '!': disallow (False)
      - Blank lines and lines starting with '#' are ignored.
    """

    def __init__(
        self,
        rules_file: Optional[Path] = None,
        inline_rules: Optional[List[str]] = None,
    ) -> None:
        self.rules_file = rules_file
        self._inline_rules = list(inline_rules) if inline_rules is not None else None
        self._lock = threading.Lock()
        self._cached_sig: Optional[Tuple[int, int]] = None
        self._cached_rules: List[RepoRule] = []
        if self._inline_rules is not None:
            self._cached_rules = self.parse_lines(self._inline_rules)

    @staticmethod
    def parse_lines(lines: List[str]) -> List[RepoRule]:
        rules: List[RepoRule] = []
        for raw_line in lines:
            line = raw_line.rstrip("\r\n")
            while line.endswith(" ") and not line.endswith("\\ "):
                line = line[:-1]
            line = line.replace("\\ ", " ")
            stripped = line.lstrip()
            if not stripped or stripped.startswith("#"):
                continue

            negated = False
            if stripped.startswith("!"):
                negated = True
                pattern = stripped[1:].strip()
            elif stripped.startswith("\\#") or stripped.startswith("\\!"):
                pattern = stripped[1:]
            else:
                pattern = stripped

            if not pattern:
                continue
            if pattern.endswith("/"):
                pattern = pattern + "*"
            pattern = pattern.lstrip("/")
            rules.append(RepoRule(pattern=pattern, negated=negated, raw=raw_line.strip()))
        return rules

    def _load_rules(self) -> List[RepoRule]:
        if self._inline_rules is not None:
            return self._cached_rules
        if self.rules_file is None:
            return []
        with self._lock:
            try:
                st = self.rules_file.stat()
                sig = (st.st_mtime_ns, st.st_size)
                if sig != self._cached_sig:
                    text = self.rules_file.read_text(encoding="utf-8")
                    self._cached_rules = self.parse_lines(text.splitlines())
                    self._cached_sig = sig
            except FileNotFoundError:
                self._cached_rules = []
                self._cached_sig = None
            return list(self._cached_rules)

    def get_rules(self) -> List[str]:
        return [r.raw for r in self._load_rules()]

    @staticmethod
    def _match_pattern(full_repo: str, pattern: str) -> bool:
        if fnmatch.fnmatchcase(full_repo, pattern):
            return True
        if pattern.endswith("/**") and full_repo.startswith(pattern[:-3] + "/"):
            return True
        return False

    def is_allowed(self, owner: str, repo: str) -> bool:
        full = f"{owner}/{repo}"
        allowed = False  # Default fail-closed: no match => disallow
        for rule in self._load_rules():
            if self._match_pattern(full, rule.pattern):
                allowed = not rule.negated
        return allowed


@dataclass
class Settings:
    host: str = "0.0.0.0"
    port: int = 8000
    public_base_url: str = ""  # e.g. https://proxy.com (recommended in production)
    trust_proxy_headers: bool = False  # Only trust X-Forwarded-* when behind a trusted reverse proxy
    temp_dir: Path = field(default_factory=default_temp_dir)
    # Default auth uses `gh auth token` (GitHub CLI). Explicit GITHUB_TOKEN env overrides if needed.
    github_token: str = ""
    use_gh_cli: bool = True
    admin_token: str = ""  # Required for PUT/DELETE /admin/* endpoints
    github_web_base: str = "https://github.com"
    github_api_base: str = "https://api.github.com"
    repo_rules_file: Optional[Path] = None
    repo_rules: Optional[List[str]] = None
    repo_aliases: Dict[str, str] = field(default_factory=dict)
    default_base_branch: str = "main"
    blocked_patch_paths: List[str] = field(default_factory=lambda: list(DEFAULT_BLOCKED_PATHS))
    allow_symlink_patches: bool = False  # Block mode 120000 symlinks by default to prevent path escapes
    max_patch_bytes: int = 10 * 1024 * 1024  # 10 MiB
    max_git_proxy_body_bytes: int = 2 * 1024 * 1024  # 2 MiB cap for git-upload-pack negotiation POST
    max_log_bytes: int = 5 * 1024 * 1024  # 5 MiB cap for downloaded Actions job logs
    git_timeout_seconds: int = 120
    # Backward-compatible alias for tests
    allow_gh_cli_fallback: bool = True

    _matcher: Optional[RepoRuleMatcher] = field(default=None, init=False, repr=False)
    _token_resolved: bool = field(default=False, init=False, repr=False)
    _cached_token: str = field(default="", init=False, repr=False)
    _token_source: str = field(default="none", init=False, repr=False)
    _token_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        self._rebuild_matcher()

    def _rebuild_matcher(self) -> None:
        if self.repo_rules_file is not None:
            self._matcher = RepoRuleMatcher(rules_file=self.repo_rules_file)
        elif self.repo_rules is not None:
            self._matcher = RepoRuleMatcher(inline_rules=self.repo_rules)
        else:
            self._matcher = RepoRuleMatcher(inline_rules=[])

    def set_rules_file(self, path: Optional[Path]) -> None:
        self.repo_rules_file = path
        self.repo_rules = None
        self._rebuild_matcher()

    def set_inline_rules(self, rules: List[str]) -> None:
        self.repo_rules_file = None
        self.repo_rules = list(rules)
        self._rebuild_matcher()

    def active_rules(self) -> List[str]:
        if self._matcher is None:
            self._rebuild_matcher()
        return self._matcher.get_rules() if self._matcher else []

    def token_source(self) -> str:
        self.resolve_github_token()
        return self._token_source

    def resolve_github_token(self) -> str:
        """Resolve GitHub token (cached so async handlers never block on subprocess).

        Priority:
          1. Explicit `github_token` (if provided via CLI/env)
          2. Default `gh auth token` via GitHub CLI (`use_gh_cli` and `allow_gh_cli_fallback`)
        """
        if self.github_token:
            self._token_source = "env/arg"
            return self.github_token
        if self._token_resolved:
            return self._cached_token
        with self._token_lock:
            if self._token_resolved:
                return self._cached_token
            tok = ""
            src = "none"
            if self.use_gh_cli and self.allow_gh_cli_fallback:
                try:
                    proc = subprocess.run(
                        ["gh", "auth", "token"],
                        capture_output=True,
                        text=True,
                        timeout=5,
                        check=False,
                    )
                    if proc.returncode == 0 and proc.stdout.strip():
                        tok = proc.stdout.strip()
                        src = "gh_cli"
                except (FileNotFoundError, subprocess.SubprocessError):
                    pass
            self._cached_token = tok
            self._token_source = src
            self._token_resolved = True
            return tok

    def is_repo_allowed(self, owner: str, repo: str) -> bool:
        if self._matcher is None:
            self._rebuild_matcher()
        return self._matcher.is_allowed(owner, repo) if self._matcher else False

    def is_patch_path_blocked(self, rel_path: str) -> bool:
        normalized = rel_path.replace("\\", "/").strip()
        if not normalized:
            return False
        # Collapse intermediate ./ and ../ segments deterministically
        normalized = posixpath.normpath(normalized).lstrip("/")
        if normalized == "." or normalized.startswith("../") or normalized == "..":
            return True
        for pat in self.blocked_patch_paths:
            pat_norm = pat.strip().lstrip("/")
            if fnmatch.fnmatchcase(normalized, pat_norm):
                return True
            if pat_norm.endswith("/**"):
                prefix = pat_norm[:-3]
                if normalized == prefix or normalized.startswith(prefix + "/"):
                    return True
        return False

    def resolve_repo_path(self, raw_path: str) -> Optional[Tuple[str, str, str]]:
        cleaned = raw_path.strip("/")
        if not cleaned:
            return None

        cleaned_no_git = cleaned[:-4] if cleaned.endswith(".git") else cleaned
        if cleaned_no_git in self.repo_aliases:
            target = self.repo_aliases[cleaned_no_git].strip("/")
            parts = target.split("/")
            if len(parts) == 2 and parts[0] and parts[1]:
                return cleaned_no_git, parts[0], parts[1]

        parts = cleaned.split("/")
        if len(parts) == 2 and parts[0] and parts[1]:
            owner = parts[0]
            repo = parts[1][:-4] if parts[1].endswith(".git") else parts[1]
            if owner and repo:
                return f"{owner}/{repo}", owner, repo

        return None

    def split_repo_and_suffix(self, full_path: str) -> Optional[Tuple[str, str, str, str]]:
        cleaned = full_path.strip("/")
        if not cleaned:
            return None

        for alias in sorted(self.repo_aliases.keys(), key=len, reverse=True):
            alias_clean = alias.strip("/")
            for candidate in (alias_clean, f"{alias_clean}.git"):
                if cleaned == candidate or cleaned.startswith(candidate + "/"):
                    suffix = cleaned[len(candidate):].lstrip("/")
                    target = self.repo_aliases[alias].strip("/")
                    tparts = target.split("/")
                    if len(tparts) == 2:
                        return alias_clean, tparts[0], tparts[1], suffix

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
    """Load Settings from environment variables (fail-closed if no rules exist)."""
    rules_file_raw = os.getenv("REPO_RULES_FILE", "").strip()
    rules_file: Optional[Path] = None
    inline_rules: Optional[List[str]] = None

    if rules_file_raw:
        rules_file = Path(rules_file_raw)
    elif Path("repos.txt").exists():
        rules_file = Path("repos.txt")
    else:
        allow_raw = os.getenv("ALLOWED_REPOS", "").strip()
        if allow_raw:
            inline_rules = [x.strip() for x in allow_raw.split(",") if x.strip()]
        else:
            # Fail-closed: points to repos.txt (if missing -> 0 rules -> deny all)
            rules_file = Path("repos.txt")

    blocked_raw = os.getenv("BLOCKED_PATCH_PATHS", "")
    blocked = (
        [x.strip() for x in blocked_raw.split(",") if x.strip()]
        if blocked_raw
        else list(DEFAULT_BLOCKED_PATHS)
    )
    if blocked_raw.strip().lower() == "none":
        blocked = []

    temp_dir_raw = os.getenv("TEMP_DIR", "").strip()
    temp_dir = Path(temp_dir_raw) if temp_dir_raw else default_temp_dir()

    return Settings(
        host=os.getenv("GATEWAY_HOST", "0.0.0.0"),
        port=int(os.getenv("GATEWAY_PORT", "8000")),
        public_base_url=os.getenv("PUBLIC_BASE_URL", "").rstrip("/"),
        trust_proxy_headers=_parse_bool_env("TRUST_PROXY_HEADERS", False),
        temp_dir=temp_dir,
        github_token=os.getenv("GITHUB_TOKEN", "").strip(),
        use_gh_cli=_parse_bool_env("USE_GH_CLI", True),
        admin_token=os.getenv("ADMIN_TOKEN", "").strip(),
        github_web_base=os.getenv("GITHUB_WEB_BASE", "https://github.com").rstrip("/"),
        github_api_base=os.getenv("GITHUB_API_BASE", "https://api.github.com").rstrip("/"),
        repo_rules_file=rules_file,
        repo_rules=inline_rules,
        repo_aliases=parse_aliases(os.getenv("REPO_ALIASES", "")),
        default_base_branch=os.getenv("DEFAULT_BASE_BRANCH", "main"),
        blocked_patch_paths=blocked,
        allow_symlink_patches=_parse_bool_env("ALLOW_SYMLINK_PATCHES", False),
        max_patch_bytes=int(os.getenv("MAX_PATCH_BYTES", str(10 * 1024 * 1024))),
        max_git_proxy_body_bytes=int(os.getenv("MAX_GIT_PROXY_BODY_BYTES", str(2 * 1024 * 1024))),
        max_log_bytes=int(os.getenv("MAX_LOG_BYTES", str(5 * 1024 * 1024))),
        git_timeout_seconds=int(os.getenv("GIT_TIMEOUT_SECONDS", "120")),
    )
