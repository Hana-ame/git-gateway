"""Configuration, gitignore-style repo allow/disallow rules, and path mapping."""

from __future__ import annotations

import fnmatch
import os
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
            # Strip CR/LF and trailing unescaped spaces (gitignore convention)
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
            # Support trailing slash shorthand like "Hana-ame/" -> "Hana-ame/*"
            if pattern.endswith("/"):
                pattern = pattern + "*"
            # Strip leading slash if user wrote "/Hana-ame/repo"
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
        allowed = False  # Default: no match => disallow
        for rule in self._load_rules():
            if self._match_pattern(full, rule.pattern):
                allowed = not rule.negated
        return allowed


@dataclass
class Settings:
    host: str = "0.0.0.0"
    port: int = 8000
    public_base_url: str = ""  # e.g. https://proxy.com (auto-inferred from request if empty)
    temp_dir: Path = field(default_factory=default_temp_dir)
    github_token: str = ""  # PAT with repo/actions:read; optional one-time fallback to `gh auth token`
    admin_token: str = ""  # Required for PUT/DELETE /admin/* endpoints
    github_web_base: str = "https://github.com"
    github_api_base: str = "https://api.github.com"
    repo_rules_file: Optional[Path] = None
    # Inline rules (used by tests or fallback when repo_rules_file is not set)
    repo_rules: Optional[List[str]] = None
    # Optional path aliases: {"path/to/repo": "Hana-ame/repo"}
    repo_aliases: Dict[str, str] = field(default_factory=dict)
    default_base_branch: str = "main"
    blocked_patch_paths: List[str] = field(default_factory=lambda: list(DEFAULT_BLOCKED_PATHS))
    max_patch_bytes: int = 10 * 1024 * 1024  # 10 MiB
    git_timeout_seconds: int = 120
    allow_gh_cli_fallback: bool = True

    _matcher: Optional[RepoRuleMatcher] = field(default=None, init=False, repr=False)
    _token_resolved: bool = field(default=False, init=False, repr=False)
    _cached_token: str = field(default="", init=False, repr=False)
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

    def resolve_github_token(self) -> str:
        """Return configured GITHUB_TOKEN or one-time cached `gh auth token` fallback.

        Cached so request handlers never block the asyncio event loop on subprocess calls.
        """
        if self.github_token:
            return self.github_token
        if self._token_resolved:
            return self._cached_token
        with self._token_lock:
            if self._token_resolved:
                return self._cached_token
            tok = ""
            if self.allow_gh_cli_fallback:
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
                except (FileNotFoundError, subprocess.SubprocessError):
                    pass
            self._cached_token = tok
            self._token_resolved = True
            return tok

    def is_repo_allowed(self, owner: str, repo: str) -> bool:
        if self._matcher is None:
            self._rebuild_matcher()
        return self._matcher.is_allowed(owner, repo) if self._matcher else False

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
        """Split a request path into (public_prefix, owner, repo, suffix).

        Matches longest configured alias first; otherwise treats the first two
        segments as `<owner>/<repo>`.
        """
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
    rules_file_raw = os.getenv("REPO_RULES_FILE", "").strip()
    rules_file: Optional[Path] = None
    inline_rules: Optional[List[str]] = None

    if rules_file_raw:
        rules_file = Path(rules_file_raw)
    elif Path("repos.txt").exists():
        rules_file = Path("repos.txt")
    else:
        # Backward-compatible fallback if REPO_RULES_FILE is not provided
        allow_raw = os.getenv("ALLOWED_REPOS", "").strip()
        if allow_raw:
            inline_rules = [x.strip() for x in allow_raw.split(",") if x.strip()]
        else:
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
        temp_dir=temp_dir,
        github_token=os.getenv("GITHUB_TOKEN", "").strip(),
        admin_token=os.getenv("ADMIN_TOKEN", "").strip(),
        github_web_base=os.getenv("GITHUB_WEB_BASE", "https://github.com").rstrip("/"),
        github_api_base=os.getenv("GITHUB_API_BASE", "https://api.github.com").rstrip("/"),
        repo_rules_file=rules_file,
        repo_rules=inline_rules,
        repo_aliases=parse_aliases(os.getenv("REPO_ALIASES", "")),
        default_base_branch=os.getenv("DEFAULT_BASE_BRANCH", "main"),
        blocked_patch_paths=blocked,
        max_patch_bytes=int(os.getenv("MAX_PATCH_BYTES", str(10 * 1024 * 1024))),
        git_timeout_seconds=int(os.getenv("GIT_TIMEOUT_SECONDS", "120")),
    )
