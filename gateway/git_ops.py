"""Ephemeral Git patch-application engine using OS temp dir (/tmp or Windows %TEMP%)."""

from __future__ import annotations

import base64
import contextlib
import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

try:
    import fcntl
except ImportError:  # Windows fallback
    fcntl = None  # type: ignore[assignment]

from .config import Settings
from .models import COMMIT_SHA_RE, build_agent_branch


logger = logging.getLogger("git_gateway.git_ops")

DIFF_GIT_RE = re.compile(r'^diff --git (?:"?a/(.+?)"?) (?:"?b/(.+?)"?)$')
PLUS_FILE_RE = re.compile(r'^\+\+\+ (?:"?(?:b/)?(.+?)"?)$')
MINUS_FILE_RE = re.compile(r'^--- (?:"?(?:a/)?(.+?)"?)$')
RENAME_RE = re.compile(r'^(?:rename|copy) (?:from|to) (?:"?(.+?)"?)$')
BASE_COMMIT_TRAILER_RE = re.compile(r"^base-commit:\s*([0-9a-fA-F]{7,40})\s*$", re.MULTILINE)


class PatchValidationError(Exception):
    def __init__(self, status_code: int, error: str, detail: str = "") -> None:
        super().__init__(detail or error)
        self.status_code = status_code
        self.error = error
        self.detail = detail


@dataclass
class ApplyResult:
    status: str  # pushed | noop
    target_branch: str
    base_branch: str
    base_commit: str
    commit_sha: str
    commit_short: str
    elapsed_ms: int


def _force_rmtree(path: Path) -> None:
    """Remove directory tree safely on both POSIX and Windows (clears read-only .git objects)."""
    def _on_error(func: Any, p: str, _exc: Any) -> None:
        try:
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
            func(p)
        except OSError:
            pass

    if path.exists():
        shutil.rmtree(path, onerror=_on_error)


def _decode_git_c_quoted(path: str) -> str:
    """Decode Git C-style quoted path including octal escapes like \147."""
    s = path.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1]
    # Remove any accidental a/ or b/ prefix after stripping quotes
    out = bytearray()
    i = 0
    n = len(s)
    simple_escapes = {
        "a": b"\a",
        "b": b"\b",
        "f": b"\f",
        "n": b"\n",
        "r": b"\r",
        "t": b"\t",
        "v": b"\v",
        "\\": b"\\",
        '"': b'"',
    }
    while i < n:
        ch = s[i]
        if ch == "\\" and i + 1 < n:
            nxt = s[i + 1]
            if nxt in simple_escapes:
                out.extend(simple_escapes[nxt])
                i += 2
                continue
            if nxt in "01234567":
                oct_digits = nxt
                j = i + 2
                while j < n and len(oct_digits) < 3 and s[j] in "01234567":
                    oct_digits += s[j]
                    j += 1
                out.append(int(oct_digits, 8) & 0xFF)
                i = j
                continue
        out.extend(ch.encode("utf-8"))
        i += 1
    return out.decode("utf-8", errors="replace")


def _strip_ab_prefix(raw: str) -> str:
    decoded = _decode_git_c_quoted(raw)
    if decoded.startswith("a/") or decoded.startswith("b/"):
        return decoded[2:]
    return decoded


def extract_touched_paths(patch_text: str) -> List[str]:
    """Best-effort pre-apply parser for file paths referenced in a patch.

    NOTE: Authoritative enforcement ALSO runs post-apply using Git's own
    `-z` tree diff (`git diff --cached` and `git diff-tree`) before push.
    """
    paths: Set[str] = set()
    for line in patch_text.splitlines():
        m = DIFF_GIT_RE.match(line)
        if m:
            paths.add(_strip_ab_prefix(m.group(1)))
            paths.add(_strip_ab_prefix(m.group(2)))
            continue
        m = PLUS_FILE_RE.match(line)
        if m:
            raw = m.group(1).split("\t", 1)[0]
            if raw != "/dev/null":
                paths.add(_strip_ab_prefix(raw))
            continue
        m = MINUS_FILE_RE.match(line)
        if m:
            raw = m.group(1).split("\t", 1)[0]
            if raw != "/dev/null":
                paths.add(_strip_ab_prefix(raw))
            continue
        m = RENAME_RE.match(line)
        if m:
            paths.add(_decode_git_c_quoted(m.group(1)))
    return sorted(paths)


def detect_base_commit(patch_text: str) -> Optional[str]:
    """Auto-detect `base-commit: <sha>` trailer emitted by `git format-patch --base=...`."""
    m = BASE_COMMIT_TRAILER_RE.search(patch_text)
    if m and COMMIT_SHA_RE.match(m.group(1)):
        return m.group(1)
    return None


def _sanitize_git_error(raw: str) -> str:
    """Strip internal temp paths and any accidental auth strings from client-facing errors."""
    lines = []
    for line in (raw or "").splitlines():
        cleaned = re.sub(r"Authorization:\s*\S+\s*\S*", "Authorization: [REDACTED]", line, flags=re.I)
        cleaned = re.sub(r'(/tmp|/var/tmp|[A-Za-z]:\\[^\s\'" ]*Temp)[^\s\'" ]*', "<temp>", cleaned, flags=re.I)
        lines.append(cleaned)
    text = "\n".join(lines).strip()
    return text[:600] if text else "git operation failed"


class GitPatchEngine:
    """Applies patches inside ephemeral OS temp directories with zero local persistence."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.temp_root = settings.temp_dir
        self.locks_dir = self.temp_root / "locks"
        self.work_dir = self.temp_root / "work"
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._locks_guard = threading.Lock()
        self._repo_locks: Dict[str, threading.Lock] = {}

    def _repo_thread_lock(self, owner: str, repo: str) -> threading.Lock:
        key = f"{owner}/{repo}"
        with self._locks_guard:
            if key not in self._repo_locks:
                self._repo_locks[key] = threading.Lock()
            return self._repo_locks[key]

    @contextlib.contextmanager
    def _repo_lock(self, owner: str, repo: str) -> Iterator[None]:
        tlock = self._repo_thread_lock(owner, repo)
        lock_path = self.locks_dir / f"{owner}__{repo}.lock"
        with tlock:
            if fcntl is None:
                yield
                return
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with open(lock_path, "a+", encoding="utf-8") as lf:
                fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

    def _remote_url(self, owner: str, repo: str) -> str:
        return f"{self.settings.github_web_base}/{owner}/{repo}.git"

    def _git_env(
        self,
        *,
        with_auth: bool = False,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        """Build subprocess env. Passes GitHub auth via GIT_CONFIG_* env vars (never argv)."""
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_ASKPASS"] = "echo"
        if with_auth:
            tok = self.settings.resolve_github_token()
            if tok:
                encoded = base64.b64encode(f"x-access-token:{tok}".encode("utf-8")).decode("ascii")
                try:
                    count = int(env.get("GIT_CONFIG_COUNT", "0"))
                except ValueError:
                    count = 0
                env[f"GIT_CONFIG_KEY_{count}"] = "http.extraHeader"
                env[f"GIT_CONFIG_VALUE_{count}"] = f"Authorization: Basic {encoded}"
                env["GIT_CONFIG_COUNT"] = str(count + 1)
        if extra_env:
            env.update(extra_env)
        return env

    def _run_git(
        self,
        args: List[str],
        *,
        cwd: Optional[Path] = None,
        with_auth: bool = False,
        extra_env: Optional[Dict[str, str]] = None,
        timeout: Optional[int] = None,
    ) -> Tuple[int, str]:
        cmd = ["git"] + args
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(cwd) if cwd else None,
                env=self._git_env(with_auth=with_auth, extra_env=extra_env),
                capture_output=True,
                text=True,
                timeout=timeout or self.settings.git_timeout_seconds,
                check=False,
            )
            combined = (proc.stdout or "") + (proc.stderr or "")
            return proc.returncode, combined
        except subprocess.TimeoutExpired:
            return -1, "git command timed out"

    def _run_git_bytes(
        self,
        args: List[str],
        *,
        cwd: Optional[Path] = None,
        timeout: Optional[int] = None,
    ) -> Tuple[int, bytes, bytes]:
        cmd = ["git"] + args
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(cwd) if cwd else None,
                env=self._git_env(with_auth=False),
                capture_output=True,
                timeout=timeout or self.settings.git_timeout_seconds,
                check=False,
            )
            return proc.returncode, proc.stdout or b"", proc.stderr or b""
        except subprocess.TimeoutExpired:
            return -1, b"", b"git command timed out"

    def _clone_ephemeral_bare(self, owner: str, repo: str, dest: Path) -> Path:
        remote_url = self._remote_url(owner, repo)
        code, out = self._run_git(
            ["clone", "--bare", remote_url, str(dest)],
            with_auth=True,
        )
        if code != 0:
            logger.error("upstream clone failed for %s/%s: %s", owner, repo, out.strip())
            raise PatchValidationError(502, "upstream clone failed", _sanitize_git_error(out))
        return dest

    def _ref_sha(self, repo_dir: Path, ref: str) -> Optional[str]:
        code, out = self._run_git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=repo_dir)
        if code == 0 and out.strip():
            return out.strip()
        return None

    def _try_apply(self, workdir: Path, patch_file: Path, use_three_way: bool = False) -> Tuple[bool, str]:
        args = ["apply", "--whitespace=nowarn"]
        if use_three_way:
            args.append("--3way")
        code, out = self._run_git(args + ["--check", str(patch_file)], cwd=workdir)
        if code != 0 and not use_three_way:
            return False, out.strip()
        code, out = self._run_git(args + [str(patch_file)], cwd=workdir)
        return code == 0, out.strip()

    def _verify_tree_security(
        self,
        workdir: Path,
        base_tree_ref: str,
        new_tree: str,
    ) -> None:
        """Authoritative post-apply verification using Git's own NUL-delimited tree diff.

        Catches C-quoted paths, octal escapes, renames, ./ segments, and mode 120000 symlinks.
        """
        code, stdout, stderr = self._run_git_bytes(
            ["diff-tree", "-r", "--raw", "-z", "--no-renames", base_tree_ref, new_tree],
            cwd=workdir,
        )
        if code != 0:
            logger.error("diff-tree failed: %s", stderr.decode("utf-8", errors="replace"))
            raise PatchValidationError(500, "security verification failed", "unable to inspect resulting tree")

        tokens = stdout.split(b"\x00")
        blocked_hits: Set[str] = set()
        symlink_hits: Set[str] = set()

        i = 0
        while i + 1 < len(tokens):
            meta = tokens[i]
            path_bytes = tokens[i + 1]
            i += 2
            if not meta:
                continue
            path_str = path_bytes.decode("utf-8", errors="replace")
            if self.settings.is_patch_path_blocked(path_str):
                blocked_hits.add(path_str)

            # meta format: ":old_mode new_mode old_sha new_sha status"
            meta_str = meta.decode("ascii", errors="replace").lstrip(":")
            parts = meta_str.split()
            if len(parts) >= 2:
                old_mode, new_mode = parts[0], parts[1]
                if not self.settings.allow_symlink_patches and (old_mode == "120000" or new_mode == "120000"):
                    symlink_hits.add(path_str)

        if blocked_hits:
            raise PatchValidationError(
                403,
                "patch modifies protected paths",
                f"blocked paths: {', '.join(sorted(blocked_hits))}",
            )
        if symlink_hits:
            raise PatchValidationError(
                403,
                "symlink modifications are blocked for security",
                f"symlink paths: {', '.join(sorted(symlink_hits))}",
            )

    def apply_and_push(
        self,
        owner: str,
        repo: str,
        *,
        nickname: str,
        base_branch: str,
        base_commit: Optional[str],
        message: str,
        patch_text: str,
        patch_mode: str = "auto",
    ) -> ApplyResult:
        t0 = time.monotonic()

        # 1. Fast-fail pre-apply security check
        touched = extract_touched_paths(patch_text)
        blocked_hits = [p for p in touched if self.settings.is_patch_path_blocked(p)]
        if blocked_hits:
            raise PatchValidationError(
                403,
                "patch modifies protected paths",
                f"blocked paths: {', '.join(blocked_hits)}",
            )

        target_branch = build_agent_branch(nickname, base_branch)
        detected_base = base_commit or detect_base_commit(patch_text)

        with self._repo_lock(owner, repo):
            self.work_dir.mkdir(parents=True, exist_ok=True)
            tmp_root = Path(tempfile.mkdtemp(prefix="ggw-req-", dir=str(self.work_dir)))
            bare_dir = tmp_root / "bare.git"
            workdir = tmp_root / "wt"
            patch_file = tmp_root / "submit.patch"

            try:
                self._clone_ephemeral_bare(owner, repo, bare_dir)

                base_branch_sha = (
                    self._ref_sha(bare_dir, f"refs/heads/{base_branch}")
                    or self._ref_sha(bare_dir, f"refs/remotes/origin/{base_branch}")
                )
                if not base_branch_sha:
                    raise PatchValidationError(
                        400,
                        "base_branch not found",
                        f"branch '{base_branch}' does not exist on {owner}/{repo}",
                    )

                existing_target_sha = (
                    self._ref_sha(bare_dir, f"refs/heads/{target_branch}")
                    or self._ref_sha(bare_dir, f"refs/remotes/origin/{target_branch}")
                )

                resolved_base_sha = base_branch_sha
                if detected_base:
                    cand = self._ref_sha(bare_dir, detected_base)
                    if cand:
                        resolved_base_sha = cand

                normalized_patch = patch_text.replace("\r\n", "\n")
                if normalized_patch and not normalized_patch.endswith("\n"):
                    normalized_patch += "\n"
                patch_file.write_text(normalized_patch, encoding="utf-8")

                start_sha = (
                    resolved_base_sha
                    if patch_mode == "cumulative"
                    else (existing_target_sha or resolved_base_sha)
                )
                code, out = self._run_git(
                    ["worktree", "add", "--detach", str(workdir), start_sha],
                    cwd=bare_dir,
                )
                if code != 0:
                    logger.error("worktree add failed: %s", out.strip())
                    raise PatchValidationError(500, "failed to create worktree", _sanitize_git_error(out))

                applied_on_existing = False
                apply_err = ""

                if existing_target_sha and patch_mode != "cumulative":
                    # 1) Try direct incremental apply on existing branch tip
                    ok, apply_err = self._try_apply(workdir, patch_file, use_three_way=False)
                    if ok:
                        applied_on_existing = True
                    else:
                        # 2) Try 3-way incremental apply on existing tip (preserves prior commits on other files!)
                        self._run_git(["reset", "--hard", existing_target_sha], cwd=workdir)
                        self._run_git(["clean", "-fdq"], cwd=workdir)
                        ok, apply_err = self._try_apply(workdir, patch_file, use_three_way=True)
                        if ok:
                            applied_on_existing = True
                        elif patch_mode == "incremental":
                            raise PatchValidationError(
                                400,
                                "incremental patch does not apply cleanly on existing branch",
                                _sanitize_git_error(apply_err),
                            )
                        else:
                            # 3) auto mode fallback: treat as cumulative snapshot from resolved_base_sha
                            self._run_git(["reset", "--hard", resolved_base_sha], cwd=workdir)
                            self._run_git(["clean", "-fdq"], cwd=workdir)
                            ok, apply_err = self._try_apply(workdir, patch_file, use_three_way=False)
                            if not ok:
                                self._run_git(["reset", "--hard", resolved_base_sha], cwd=workdir)
                                self._run_git(["clean", "-fdq"], cwd=workdir)
                                ok, apply_err = self._try_apply(workdir, patch_file, use_three_way=True)
                            if not ok:
                                raise PatchValidationError(
                                    400, "patch does not apply cleanly", _sanitize_git_error(apply_err)
                                )
                else:
                    ok, apply_err = self._try_apply(workdir, patch_file, use_three_way=False)
                    if not ok:
                        self._run_git(["reset", "--hard", resolved_base_sha], cwd=workdir)
                        self._run_git(["clean", "-fdq"], cwd=workdir)
                        ok, apply_err = self._try_apply(workdir, patch_file, use_three_way=True)
                    if not ok:
                        raise PatchValidationError(
                            400, "patch does not apply cleanly", _sanitize_git_error(apply_err)
                        )

                self._run_git(["add", "-A"], cwd=workdir)

                tree_code, tree_out = self._run_git(["write-tree"], cwd=workdir)
                if tree_code != 0:
                    logger.error("git write-tree failed: %s", tree_out.strip())
                    raise PatchValidationError(500, "git write-tree failed", _sanitize_git_error(tree_out))
                new_tree = tree_out.strip()

                parent_sha = existing_target_sha if existing_target_sha else resolved_base_sha

                # 2. Authoritative post-apply security verification against BOTH parent and base_branch
                self._verify_tree_security(workdir, parent_sha, new_tree)
                if parent_sha != base_branch_sha:
                    self._verify_tree_security(workdir, base_branch_sha, new_tree)

                parent_tree_code, parent_tree_out = self._run_git(
                    ["rev-parse", f"{parent_sha}^{{tree}}"], cwd=workdir
                )
                if parent_tree_code == 0 and parent_tree_out.strip() == new_tree:
                    return ApplyResult(
                        status="noop",
                        target_branch=target_branch,
                        base_branch=base_branch,
                        base_commit=resolved_base_sha,
                        commit_sha=parent_sha,
                        commit_short=parent_sha[:7],
                        elapsed_ms=int((time.monotonic() - t0) * 1000),
                    )

                commit_msg = (
                    f"{message.strip()}\n\n"
                    f"Submitted-by: {nickname}\n"
                    f"Base-branch: {base_branch}\n"
                    f"Base-commit: {resolved_base_sha}\n"
                )
                if existing_target_sha and not applied_on_existing:
                    commit_msg += "Patch-mode: cumulative-snapshot\n"

                author_name = f"{nickname} (agent)"
                author_email = f"{nickname}@agents.local"
                identity_env = {
                    "GIT_AUTHOR_NAME": author_name,
                    "GIT_AUTHOR_EMAIL": author_email,
                    "GIT_COMMITTER_NAME": author_name,
                    "GIT_COMMITTER_EMAIL": author_email,
                }

                c_code, c_out = self._run_git(
                    ["commit-tree", new_tree, "-p", parent_sha, "-m", commit_msg],
                    cwd=workdir,
                    extra_env=identity_env,
                )
                if c_code != 0:
                    logger.error("git commit-tree failed: %s", c_out.strip())
                    raise PatchValidationError(500, "git commit-tree failed", _sanitize_git_error(c_out))
                new_sha = c_out.strip()

                p_code, p_out = self._run_git(
                    ["push", "origin", f"{new_sha}:refs/heads/{target_branch}"],
                    cwd=bare_dir,
                    with_auth=True,
                )
                if p_code != 0:
                    logger.error("git push failed for %s/%s: %s", owner, repo, p_out.strip())
                    raise PatchValidationError(502, "push to upstream failed", _sanitize_git_error(p_out))

                return ApplyResult(
                    status="pushed",
                    target_branch=target_branch,
                    base_branch=base_branch,
                    base_commit=resolved_base_sha,
                    commit_sha=new_sha,
                    commit_short=new_sha[:7],
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                )
            finally:
                _force_rmtree(tmp_root)
