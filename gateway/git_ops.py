"""Ephemeral Git patch-application engine using OS temp dir (/tmp or Windows %TEMP%)."""

from __future__ import annotations

import base64
import contextlib
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


DIFF_GIT_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")
PLUS_FILE_RE = re.compile(r"^\+\+\+ (?:b/)?(.+)$")
MINUS_FILE_RE = re.compile(r"^--- (?:a/)?(.+)$")
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


def extract_touched_paths(patch_text: str) -> List[str]:
    """Parse all file paths referenced by a unified diff or format-patch."""
    paths: Set[str] = set()
    for line in patch_text.splitlines():
        m = DIFF_GIT_RE.match(line)
        if m:
            paths.add(m.group(1).strip().strip('"'))
            paths.add(m.group(2).strip().strip('"'))
            continue
        m = PLUS_FILE_RE.match(line)
        if m:
            p = m.group(1).strip().strip('"')
            if p != "/dev/null":
                paths.add(p)
            continue
        m = MINUS_FILE_RE.match(line)
        if m:
            p = m.group(1).strip().strip('"')
            if p != "/dev/null":
                paths.add(p)
    return sorted(paths)


def detect_base_commit(patch_text: str) -> Optional[str]:
    """Auto-detect `base-commit: <sha>` trailer emitted by `git format-patch --base=...`."""
    m = BASE_COMMIT_TRAILER_RE.search(patch_text)
    if m and COMMIT_SHA_RE.match(m.group(1)):
        return m.group(1)
    return None


class GitPatchEngine:
    r"""Applies patches inside ephemeral OS temp directories with zero local persistence.

    All temporary bare clones, worktrees, patch files, and lockfiles live strictly under
    `settings.temp_dir` (defaults to `/tmp/git-gateway` on Linux/macOS or `%TEMP%\git-gateway`
    on Windows) and every request workspace is deleted immediately in `finally`.
    """

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

    def _git_auth_args(self) -> List[str]:
        tok = self.settings.resolve_github_token()
        if not tok:
            return []
        encoded = base64.b64encode(f"x-access-token:{tok}".encode("utf-8")).decode("ascii")
        return ["-c", f"http.extraHeader=Authorization: Basic {encoded}"]

    def _git_env(self) -> Dict[str, str]:
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_ASKPASS"] = "echo"
        return env

    def _run_git(
        self,
        args: List[str],
        *,
        cwd: Optional[Path] = None,
        with_auth: bool = False,
        timeout: Optional[int] = None,
    ) -> Tuple[int, str]:
        cmd = ["git"]
        if with_auth:
            cmd.extend(self._git_auth_args())
        cmd.extend(args)
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(cwd) if cwd else None,
                env=self._git_env(),
                capture_output=True,
                text=True,
                timeout=timeout or self.settings.git_timeout_seconds,
                check=False,
            )
            combined = (proc.stdout or "") + (proc.stderr or "")
            return proc.returncode, combined
        except subprocess.TimeoutExpired:
            return -1, "git command timed out"

    def _clone_ephemeral_bare(self, owner: str, repo: str, dest: Path) -> Path:
        remote_url = self._remote_url(owner, repo)
        code, out = self._run_git(
            ["clone", "--bare", remote_url, str(dest)],
            with_auth=True,
        )
        if code != 0:
            raise PatchValidationError(502, "upstream clone failed", out.strip())
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
    ) -> ApplyResult:
        t0 = time.monotonic()

        # 1. Security guardrail: reject patches modifying blocked paths
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

                start_sha = existing_target_sha or resolved_base_sha
                code, out = self._run_git(
                    ["worktree", "add", "--detach", str(workdir), start_sha],
                    cwd=bare_dir,
                )
                if code != 0:
                    raise PatchValidationError(500, "failed to create worktree", out.strip())

                author_name = f"{nickname} (agent)"
                author_email = f"{nickname}@agents.local"
                self._run_git(["config", "user.name", author_name], cwd=workdir)
                self._run_git(["config", "user.email", author_email], cwd=workdir)

                applied_on_existing = False
                apply_err = ""

                if existing_target_sha:
                    ok, apply_err = self._try_apply(workdir, patch_file, use_three_way=False)
                    if ok:
                        applied_on_existing = True
                    else:
                        self._run_git(["reset", "--hard", resolved_base_sha], cwd=workdir)
                        self._run_git(["clean", "-fdq"], cwd=workdir)
                        ok, apply_err = self._try_apply(workdir, patch_file, use_three_way=False)
                        if not ok:
                            self._run_git(["reset", "--hard", resolved_base_sha], cwd=workdir)
                            self._run_git(["clean", "-fdq"], cwd=workdir)
                            ok, apply_err = self._try_apply(workdir, patch_file, use_three_way=True)
                        if not ok:
                            raise PatchValidationError(400, "patch does not apply cleanly", apply_err)
                else:
                    ok, apply_err = self._try_apply(workdir, patch_file, use_three_way=False)
                    if not ok:
                        self._run_git(["reset", "--hard", resolved_base_sha], cwd=workdir)
                        self._run_git(["clean", "-fdq"], cwd=workdir)
                        ok, apply_err = self._try_apply(workdir, patch_file, use_three_way=True)
                    if not ok:
                        raise PatchValidationError(400, "patch does not apply cleanly", apply_err)

                self._run_git(["add", "-A"], cwd=workdir)

                tree_code, tree_out = self._run_git(["write-tree"], cwd=workdir)
                if tree_code != 0:
                    raise PatchValidationError(500, "git write-tree failed", tree_out.strip())
                new_tree = tree_out.strip()

                parent_sha = existing_target_sha if existing_target_sha else resolved_base_sha
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

                c_code, c_out = self._run_git(
                    ["commit-tree", new_tree, "-p", parent_sha, "-m", commit_msg],
                    cwd=workdir,
                )
                if c_code != 0:
                    raise PatchValidationError(500, "git commit-tree failed", c_out.strip())
                new_sha = c_out.strip()

                p_code, p_out = self._run_git(
                    ["push", "origin", f"{new_sha}:refs/heads/{target_branch}"],
                    cwd=bare_dir,
                    with_auth=True,
                )
                if p_code != 0:
                    raise PatchValidationError(502, "push to upstream failed", p_out.strip())

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
