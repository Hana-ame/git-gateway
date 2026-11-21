"""Async GitHub REST API client for PR creation/reuse, CI/CD checks, and failed job error logs."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

import httpx

from .config import Settings
from .models import CIJobSummary, PRCIStatusResponse


ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
FAILURE_CONCLUSIONS = {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}
SUCCESS_CONCLUSIONS = {"success", "neutral", "skipped"}


class GitHubAPIError(RuntimeError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def strip_ansi(text: str) -> str:
    return ANSI_ESCAPE_RE.sub("", text)


def tail_lines(text: str, max_lines: int) -> str:
    if max_lines <= 0:
        return text
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    omitted = len(lines) - max_lines
    return f"... ({omitted} earlier lines omitted; pass ?tail=0 for full log) ...\n" + "\n".join(
        lines[-max_lines:]
    )


class GitHubClient:
    """Thin async GitHub REST API v3 client using server-side token authentication."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "git-gateway/2.0",
        }
        tok = self.settings.resolve_github_token()
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        timeout: float = 30.0,
    ) -> httpx.Response:
        url = f"{self.settings.github_api_base}{path}"
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            resp = await client.request(
                method,
                url,
                headers=self._headers(),
                params=params,
                json=json_body,
            )
        if resp.status_code >= 400:
            detail = resp.text[:500]
            try:
                payload = resp.json()
                if isinstance(payload, dict) and payload.get("message"):
                    detail = str(payload["message"])
                    if payload.get("errors"):
                        detail += f" ({payload['errors']})"
            except Exception:
                pass
            raise GitHubAPIError(resp.status_code, detail)
        return resp

    async def ensure_pr(
        self,
        owner: str,
        repo: str,
        *,
        head_branch: str,
        base_branch: str,
        title: str,
        body: str,
    ) -> Tuple[int, str, bool]:
        """Find an existing open PR for head->base, or create a new one.

        Returns: (pr_number, pr_html_url, reused_existing)
        """
        existing = await self._request(
            "GET",
            f"/repos/{owner}/{repo}/pulls",
            params={
                "state": "open",
                "head": f"{owner}:{head_branch}",
                "base": base_branch,
                "per_page": 5,
            },
        )
        prs = existing.json()
        if isinstance(prs, list) and prs:
            pr = prs[0]
            return int(pr["number"]), str(pr.get("html_url", "")), True

        created = await self._request(
            "POST",
            f"/repos/{owner}/{repo}/pulls",
            json_body={
                "title": title,
                "head": head_branch,
                "base": base_branch,
                "body": body,
            },
        )
        pr = created.json()
        return int(pr["number"]), str(pr.get("html_url", "")), False

    async def list_prs(
        self,
        owner: str,
        repo: str,
        *,
        state: str = "open",
        nickname: Optional[str] = None,
        base_branch: Optional[str] = None,
        limit: int = 30,
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {
            "state": state,
            "per_page": max(1, min(limit, 100)),
        }
        if base_branch:
            params["base"] = base_branch
        resp = await self._request("GET", f"/repos/{owner}/{repo}/pulls", params=params)
        items = resp.json()
        if not isinstance(items, list):
            return []

        results: List[Dict[str, Any]] = []
        nick_prefix = f"{nickname}/" if nickname else None
        for pr in items:
            head_ref = str((pr.get("head") or {}).get("ref", ""))
            if nick_prefix and not head_ref.startswith(nick_prefix):
                continue
            results.append(
                {
                    "number": pr.get("number"),
                    "title": pr.get("title"),
                    "state": pr.get("state"),
                    "head_branch": head_ref,
                    "base_branch": (pr.get("base") or {}).get("ref"),
                    "head_sha": (pr.get("head") or {}).get("sha"),
                    "url": pr.get("html_url"),
                    "updated_at": pr.get("updated_at"),
                }
            )
        return results

    async def get_pr(self, owner: str, repo: str, pr_number: int) -> Dict[str, Any]:
        resp = await self._request("GET", f"/repos/{owner}/{repo}/pulls/{pr_number}")
        return resp.json()

    async def get_commit_check_runs(self, owner: str, repo: str, sha: str) -> List[Dict[str, Any]]:
        resp = await self._request(
            "GET",
            f"/repos/{owner}/{repo}/commits/{sha}/check-runs",
            params={"per_page": 100},
        )
        data = resp.json()
        runs = data.get("check_runs", []) if isinstance(data, dict) else []
        return runs if isinstance(runs, list) else []

    async def get_workflow_jobs_for_sha(self, owner: str, repo: str, sha: str) -> List[Dict[str, Any]]:
        runs_resp = await self._request(
            "GET",
            f"/repos/{owner}/{repo}/actions/runs",
            params={"head_sha": sha, "per_page": 20},
        )
        runs_data = runs_resp.json()
        workflow_runs = runs_data.get("workflow_runs", []) if isinstance(runs_data, dict) else []
        all_jobs: List[Dict[str, Any]] = []
        for run in workflow_runs:
            run_id = run.get("id")
            if not run_id:
                continue
            jobs_resp = await self._request(
                "GET",
                f"/repos/{owner}/{repo}/actions/runs/{run_id}/jobs",
                params={"per_page": 100},
            )
            jobs_data = jobs_resp.json()
            jobs = jobs_data.get("jobs", []) if isinstance(jobs_data, dict) else []
            for job in jobs:
                job["_workflow_name"] = run.get("name")
                all_jobs.append(job)
        return all_jobs

    async def get_job_logs(
        self,
        owner: str,
        repo: str,
        job_id: int,
        *,
        tail: int = 200,
    ) -> str:
        """Fetch raw Actions job logs, following redirect without leaking auth token."""
        url = f"{self.settings.github_api_base}/repos/{owner}/{repo}/actions/jobs/{job_id}/logs"
        async with httpx.AsyncClient(timeout=45.0, follow_redirects=False) as client:
            resp = await client.get(url, headers=self._headers())
            if resp.status_code in (301, 302, 303, 307, 308):
                loc = resp.headers.get("location")
                if not loc:
                    raise GitHubAPIError(502, "missing redirect location for job logs")
                # Do NOT forward GitHub Bearer token to external pre-signed blob URL
                resp = await client.get(loc, headers={"User-Agent": "git-gateway/2.0"})
            if resp.status_code >= 400:
                raise GitHubAPIError(resp.status_code, resp.text[:400])
            clean = strip_ansi(resp.text)
            return tail_lines(clean, tail)

    async def get_pr_ci_status(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        *,
        repo_base_url: str,
    ) -> PRCIStatusResponse:
        pr = await self.get_pr(owner, repo, pr_number)
        head = pr.get("head") or {}
        base = pr.get("base") or {}
        head_sha = str(head.get("sha", ""))

        check_runs_raw = await self.get_commit_check_runs(owner, repo, head_sha) if head_sha else []
        jobs_raw: List[Dict[str, Any]] = []
        if head_sha:
            try:
                jobs_raw = await self.get_workflow_jobs_for_sha(owner, repo, head_sha)
            except GitHubAPIError:
                jobs_raw = []

        passed = 0
        failed = 0
        pending = 0
        check_summaries: List[Dict[str, Any]] = []
        for cr in check_runs_raw:
            status = str(cr.get("status") or "")
            conclusion = cr.get("conclusion")
            if status != "completed":
                pending += 1
            elif conclusion in FAILURE_CONCLUSIONS:
                failed += 1
            elif conclusion in SUCCESS_CONCLUSIONS:
                passed += 1
            else:
                pending += 1
            check_summaries.append(
                {
                    "id": cr.get("id"),
                    "name": cr.get("name"),
                    "status": status,
                    "conclusion": conclusion,
                    "details_url": cr.get("details_url") or cr.get("html_url"),
                }
            )

        failed_jobs: List[CIJobSummary] = []
        for job in jobs_raw:
            conclusion = job.get("conclusion")
            if conclusion in FAILURE_CONCLUSIONS:
                failed_steps = [
                    str(s.get("name"))
                    for s in (job.get("steps") or [])
                    if s.get("conclusion") in FAILURE_CONCLUSIONS
                ]
                jid = int(job["id"])
                failed_jobs.append(
                    CIJobSummary(
                        job_id=jid,
                        name=str(job.get("name", f"job-{jid}")),
                        status=str(job.get("status", "completed")),
                        conclusion=str(conclusion),
                        html_url=job.get("html_url"),
                        failed_steps=failed_steps,
                        logs_url=f"{repo_base_url}/actions/jobs/{jid}/logs",
                    )
                )

        if failed > 0 or failed_jobs:
            overall = "failed"
        elif pending > 0:
            overall = "running"
        elif passed > 0:
            overall = "passed"
        else:
            overall = "none"

        logs_url = f"{repo_base_url}/prs/{pr_number}/logs"
        if overall == "passed":
            hint = "All CI/CD checks passed. Your PR is ready for maintainer review."
        elif overall == "failed":
            hint = f"CI/CD failed. Fetch error logs via GET {logs_url} , fix locally, and resubmit a patch."
        elif overall == "running":
            hint = "CI/CD checks are still running. Poll this endpoint again in 15-30 seconds."
        else:
            hint = "No CI/CD check runs reported yet. Wait a few seconds and poll again."

        return PRCIStatusResponse(
            ok=True,
            repo=f"{owner}/{repo}",
            pr_number=pr_number,
            title=str(pr.get("title", "")),
            state=str(pr.get("state", "")),
            head_branch=str(head.get("ref", "")),
            base_branch=str(base.get("ref", "")),
            head_sha=head_sha,
            pr_url=str(pr.get("html_url", "")),
            mergeable=pr.get("mergeable"),
            overall_status=overall,
            passed_count=passed,
            failed_count=failed,
            pending_count=pending,
            check_runs=check_summaries,
            failed_jobs=failed_jobs,
            logs_url=logs_url,
            hint=hint,
        )

    async def get_pr_logs(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        *,
        tail: int = 200,
        include_all: bool = False,
    ) -> Dict[str, Any]:
        pr = await self.get_pr(owner, repo, pr_number)
        head_sha = str((pr.get("head") or {}).get("sha", ""))
        if not head_sha:
            raise GitHubAPIError(404, f"PR #{pr_number} has no head commit SHA")

        jobs = await self.get_workflow_jobs_for_sha(owner, repo, head_sha)
        target_jobs = (
            jobs
            if include_all
            else [j for j in jobs if j.get("conclusion") in FAILURE_CONCLUSIONS]
        )

        entries: List[Dict[str, Any]] = []
        for job in target_jobs:
            jid = int(job["id"])
            failed_steps = [
                str(s.get("name"))
                for s in (job.get("steps") or [])
                if s.get("conclusion") in FAILURE_CONCLUSIONS
            ]
            try:
                log_text = await self.get_job_logs(owner, repo, jid, tail=tail)
            except GitHubAPIError as exc:
                log_text = f"(unable to fetch log for job {jid}: {exc.message})"
            entries.append(
                {
                    "job_id": jid,
                    "name": job.get("name"),
                    "workflow": job.get("_workflow_name"),
                    "status": job.get("status"),
                    "conclusion": job.get("conclusion"),
                    "failed_steps": failed_steps,
                    "log": log_text,
                }
            )

        return {
            "ok": True,
            "repo": f"{owner}/{repo}",
            "pr_number": pr_number,
            "head_sha": head_sha,
            "job_count": len(entries),
            "jobs": entries,
        }
