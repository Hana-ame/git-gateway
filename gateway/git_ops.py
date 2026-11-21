"""Isolated Git patch-application engine with per-repo thread + cross-process (flock) locking."""

from __future__ import annotations

import base64
import contextlib
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple

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
    """Applies patches in ephemeral worktrees backed by per-repo bare mirrors.

    Concurrency model:
      - In-process: per-repo `threading.Lock` serializes concurrent worker threads
        targeting the same repository while allowing distinct repositories to run
        in parallel.
      - Cross-process: per-repo POSIX `fcntl.flock` lockfile guarantees safety
        even when deployed behind multi-worker Uvicorn (`--workers N`).
      - Isolation: every submission runs in a fresh `mkdtemp` detached worktree
        that is always removed and pruned in `finally`.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.mirrors_dir = settings.data_dir / "mirrors"
        self.mirrors_dir.mkdir(parents=True, exist_ok=True)
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
        lock_path = self.mirrors_dir / f"{owner}__{repo}.lock"
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

    def _ensure_mirror(self, owner: str, repo: str) -> Path:
        mirror_path = self.mirrors_dir / f"{owner}__{repo}.git"
        remote_url = self._remote_url(owner, repo)
        if not (mirror_path / "HEAD").exists():
            if mirror_path.exists():
                shutil.rmtree(mirror_path, ignore_errors=True)
            code, out = self._run_git(
                ["clone", "--bare", remote_url, str(mirror_path)],
                with_auth=True,
            )
            if code != 0:
                shutil.rmtree(mirror_path, ignore_errors=True)
                raise PatchValidationError(502, "upstream clone failed", out.strip())
            self._run_git(
                ["config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"],
                cwd=mirror_path,
            )
        else:
            self._run_git(["remote", "set-url", "origin", remote_url], cwd=mirror_path)

        code, out = self._run_git(
            ["fetch", "origin", "--prune", "--quiet"],
            cwd=mirror_path,
            with_auth=True,
        )
        if code != 0:
            raise PatchValidationError(502, "upstream fetch failed", out.strip())
        return mirror_path

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
            mirror = self._ensure_mirror(owner, repo)

            base_branch_sha = (
                self._ref_sha(mirror, f"refs/remotes/origin/{base_branch}")
                or self._ref_sha(mirror, f"refs/heads/{base_branch}")
            )
            if not base_branch_sha:
                raise PatchValidationError(
                    400,
                    "base_branch not found",
                    f"branch '{base_branch}' does not exist on {owner}/{repo}",
                )

            existing_target_sha = (
                self._ref_sha(mirror, f"refs/remotes/origin/{target_branch}")
                or self._ref_sha(mirror, f"refs/heads/{target_branch}")
            )

            resolved_base_sha = base_branch_sha
            if detected_base:
                cand = self._ref_sha(mirror, detected_base)
                if cand:
                    resolved_base_sha = cand

            tmp_root = Path(tempfile.mkdtemp(prefix="ggw-work-"))
            workdir = tmp_root / "wt"
            patch_file = tmp_root / "submit.patch"
            normalized_patch = patch_text.replace("\r\n", "\n")
            if normalized_patch and not normalized_patch.endswith("\n"):
                normalized_patch += "\n"
            patch_file.write_text(normalized_patch, encoding="utf-8")

            try:
                start_sha = existing_target_sha or resolved_base_sha
                code, out = self._run_git(
                    ["worktree", "add", "--detach", str(workdir), start_sha],
                    cwd=mirror,
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
                            raise PatchValidationError(
                                400,
                                "patch does not apply cleanly",
                                apply_err,
                            )
                else:
                    ok, apply_err = self._try_apply(workdir, patch_file, use_three_way=False)
                    if not ok:
                        self._run_git(["reset", "--hard", resolved_base_sha], cwd=workdir)
                        self._run_git(["clean", "-fdq"], cwd=workdir)
                        ok, apply_err = self._try_apply(workdir, patch_file, use_three_way=True)
                    if not ok:
                        raise PatchValidationError(
                            400,
                            "patch does not apply cleanly",
                            apply_err,
                        )

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
                    short = parent_sha[:7]
                    return ApplyResult(
                        status="noop",
                        target_branch=target_branch,
                        base_branch=base_branch,
                        base_commit=resolved_base_sha,
                        commit_sha=parent_sha,
                        commit_short=short,
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
                short_sha = new_sha[:7]

                p_code, p_out = self._run_git(
                    ["push", "origin", f"{new_sha}:refs/heads/{target_branch}"],
                    cwd=mirror,
                    with_auth=True,
                )
                if p_code != 0:
                    raise PatchValidationError(502, "push to upstream failed", p_out.strip())

                self._run_git(
                    ["update-ref", f"refs/remotes/origin/{target_branch}", new_sha],
                    cwd=mirror,
                )

                return ApplyResult(
                    status="pushed",
                    target_branch=target_branch,
                    base_branch=base_branch,
                    base_commit=resolved_base_sha,
                    commit_sha=new_sha,
                    commit_short=short_sha,
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                )
            finally:
                self._run_git(["worktree", "remove", "--force", str(workdir)], cwd=mirror)
                self._run_git(["worktree", "prune"], cwd=mirror)
                shutil.rmtree(tmp_root, ignore_errors=True)
