// src/githubClient.js — Zero-dependency GitHub REST API v3 client using native fetch
'use strict';

const ANSI_ESCAPE_RE = /\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])/g;
const FAILURE_CONCLUSIONS = new Set(['failure', 'timed_out', 'cancelled', 'action_required', 'startup_failure']);
const SUCCESS_CONCLUSIONS = new Set(['success', 'neutral', 'skipped']);

class GitHubAPIError extends Error {
  constructor(statusCode, message) {
    super(message);
    this.statusCode = statusCode;
    this.message = message;
  }
}

function stripAnsi(text) {
  return String(text || '').replace(ANSI_ESCAPE_RE, '');
}

function tailLines(text, maxLines) {
  if (!maxLines || maxLines <= 0) return text;
  const lines = String(text || '').split(/\r?\n/);
  if (lines.length <= maxLines) return text;
  const omitted = lines.length - maxLines;
  return `... (${omitted} earlier lines omitted; pass ?tail=0 for full log) ...\n` + lines.slice(-maxLines).join('\n');
}

class GitHubClient {
  constructor(settings) {
    this.settings = settings;
  }

  _headers() {
    const headers = {
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
      'User-Agent': 'git-gateway/3.0-node',
    };
    const tok = this.settings.resolveGithubToken();
    if (tok) headers.Authorization = `Bearer ${tok}`;
    return headers;
  }

  async _request(method, apiPath, { params = null, jsonBody = null, timeoutMs = 30000 } = {}) {
    const url = new URL(`${this.settings.githubApiBase}${apiPath}`);
    if (params) {
      for (const [k, v] of Object.entries(params)) {
        if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, String(v));
      }
    }
    const headers = this._headers();
    let body;
    if (jsonBody !== null) {
      headers['Content-Type'] = 'application/json';
      body = JSON.stringify(jsonBody);
    }

    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    let resp;
    try {
      resp = await fetch(url.toString(), {
        method,
        headers,
        body,
        redirect: 'manual',
        signal: ctrl.signal,
      });
    } catch (err) {
      throw new GitHubAPIError(502, `GitHub network error: ${err.message}`);
    } finally {
      clearTimeout(timer);
    }

    if (resp.status >= 400) {
      const text = await resp.text().catch(() => '');
      let detail = text.slice(0, 500);
      try {
        const payload = JSON.parse(text);
        if (payload && typeof payload === 'object' && payload.message) {
          detail = String(payload.message);
          if (payload.errors) detail += ` (${JSON.stringify(payload.errors)})`;
        }
      } catch {
        // keep raw snippet
      }
      throw new GitHubAPIError(resp.status, detail);
    }
    return resp;
  }

  async _findOpenPr(owner, repo, headBranch, baseBranch) {
    const resp = await this._request('GET', `/repos/${owner}/${repo}/pulls`, {
      params: { state: 'open', head: `${owner}:${headBranch}`, base: baseBranch, per_page: 5 },
    });
    const prs = await resp.json();
    if (Array.isArray(prs) && prs.length > 0) {
      return { number: Number(prs[0].number), url: String(prs[0].html_url || '') };
    }
    return null;
  }

  async ensurePr(owner, repo, { headBranch, baseBranch, title, body }) {
    const found = await this._findOpenPr(owner, repo, headBranch, baseBranch);
    if (found) return { prNumber: found.number, prUrl: found.url, reused: true };

    try {
      const created = await this._request('POST', `/repos/${owner}/${repo}/pulls`, {
        jsonBody: { title, head: headBranch, base: baseBranch, body },
      });
      const pr = await created.json();
      return { prNumber: Number(pr.number), prUrl: String(pr.html_url || ''), reused: false };
    } catch (err) {
      if (err instanceof GitHubAPIError && err.statusCode === 422) {
        const retry = await this._findOpenPr(owner, repo, headBranch, baseBranch);
        if (retry) return { prNumber: retry.number, prUrl: retry.url, reused: true };
      }
      throw err;
    }
  }

  async listPrs(owner, repo, { state = 'open', nickname = null, baseBranch = null, limit = 30 } = {}) {
    const capped = Math.max(1, Math.min(limit, 100));
    const nickPrefix = nickname ? `${nickname}/` : null;
    const results = [];
    const maxPages = nickPrefix ? 5 : 1;
    const perPage = nickPrefix ? 100 : capped;

    for (let page = 1; page <= maxPages; page++) {
      const resp = await this._request('GET', `/repos/${owner}/${repo}/pulls`, {
        params: { state, per_page: perPage, page, base: baseBranch || undefined },
      });
      const items = await resp.json();
      if (!Array.isArray(items) || items.length === 0) break;

      for (const pr of items) {
        const headRef = String((pr.head && pr.head.ref) || '');
        if (nickPrefix && !headRef.startsWith(nickPrefix)) continue;
        results.push({
          number: pr.number,
          title: pr.title,
          state: pr.state,
          head_branch: headRef,
          base_branch: pr.base ? pr.base.ref : null,
          head_sha: pr.head ? pr.head.sha : null,
          html_url: pr.html_url,
          url: pr.html_url,
          updated_at: pr.updated_at,
        });
        if (results.length >= capped) return results;
      }
      if (items.length < perPage) break;
    }
    return results;
  }

  async getPr(owner, repo, prNumber) {
    const resp = await this._request('GET', `/repos/${owner}/${repo}/pulls/${prNumber}`);
    return resp.json();
  }

  async getCommitCheckRuns(owner, repo, sha) {
    const resp = await this._request('GET', `/repos/${owner}/${repo}/commits/${sha}/check-runs`, {
      params: { per_page: 100 },
    });
    const data = await resp.json();
    return Array.isArray(data && data.check_runs) ? data.check_runs : [];
  }

  static _selectLatestWorkflowRuns(workflowRuns) {
    const latestByWf = new Map();
    for (const run of workflowRuns || []) {
      const key = run.workflow_id || run.name || run.id;
      const rank = [Number(run.run_number || 0), Number(run.run_attempt || 0), Number(run.id || 0)];
      const prev = latestByWf.get(key);
      if (!prev) {
        latestByWf.set(key, { run, rank });
      } else {
        const pr = prev.rank;
        const newer = rank[0] > pr[0] || (rank[0] === pr[0] && (rank[1] > pr[1] || (rank[1] === pr[1] && rank[2] >= pr[2])));
        if (newer) latestByWf.set(key, { run, rank });
      }
    }
    return Array.from(latestByWf.values()).map((x) => x.run);
  }

  async getWorkflowJobsForSha(owner, repo, sha) {
    const runsResp = await this._request('GET', `/repos/${owner}/${repo}/actions/runs`, {
      params: { head_sha: sha, per_page: 30 },
    });
    const runsData = await runsResp.json();
    const latestRuns = GitHubClient._selectLatestWorkflowRuns(runsData && runsData.workflow_runs);

    const allJobs = [];
    for (const run of latestRuns) {
      if (!run.id) continue;
      const jobsResp = await this._request('GET', `/repos/${owner}/${repo}/actions/runs/${run.id}/jobs`, {
        params: { per_page: 100 },
      });
      const jobsData = await jobsResp.json();
      for (const job of (jobsData && jobsData.jobs) || []) {
        allJobs.push({ ...job, _workflow_name: run.name });
      }
    }
    return allJobs;
  }

  async _downloadBoundedText(url, headers, maxBytes) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 45000);
    let resp;
    try {
      resp = await fetch(url, { method: 'GET', headers, redirect: 'manual', signal: ctrl.signal });
    } catch (err) {
      throw new GitHubAPIError(502, `GitHub log fetch network error: ${err.message}`);
    } finally {
      clearTimeout(timer);
    }

    if ([301, 302, 303, 307, 308].includes(resp.status)) {
      return { status: resp.status, text: '', location: resp.headers.get('location') };
    }

    const chunks = [];
    let total = 0;
    let truncated = false;
    if (resp.body) {
      for await (const chunk of resp.body) {
        const buf = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
        const remaining = maxBytes - total;
        if (remaining <= 0) {
          truncated = true;
          break;
        }
        if (buf.length > remaining) {
          chunks.push(buf.subarray(0, remaining));
          total += remaining;
          truncated = true;
          break;
        }
        chunks.push(buf);
        total += buf.length;
      }
    }
    let text = Buffer.concat(chunks).toString('utf-8');
    if (truncated) text += `\n... (log truncated at ${maxBytes} bytes limit) ...\n`;
    return { status: resp.status, text, location: null };
  }

  async getJobLogs(owner, repo, jobId, { tail = 200 } = {}) {
    const url = `${this.settings.githubApiBase}/repos/${owner}/${repo}/actions/jobs/${jobId}/logs`;
    const maxBytes = this.settings.maxLogBytes;
    let { status, text, location } = await this._downloadBoundedText(url, this._headers(), maxBytes);
    if ([301, 302, 303, 307, 308].includes(status)) {
      if (!location) throw new GitHubAPIError(502, 'missing redirect location for job logs');
      // Never forward GitHub Authorization token to external pre-signed blob URLs
      ({ status, text } = await this._downloadBoundedText(location, { 'User-Agent': 'git-gateway/3.0-node' }, maxBytes));
    }
    if (status >= 400) throw new GitHubAPIError(status, text.slice(0, 400));
    return tailLines(stripAnsi(text), tail);
  }

  async getPrCiStatus(owner, repo, prNumber, { repoBaseUrl }) {
    const pr = await this.getPr(owner, repo, prNumber);
    const head = pr.head || {};
    const base = pr.base || {};
    const headSha = String(head.sha || '');

    const checkRunsRaw = headSha ? await this.getCommitCheckRuns(owner, repo, headSha) : [];
    let jobsRaw = [];
    if (headSha) {
      try {
        jobsRaw = await this.getWorkflowJobsForSha(owner, repo, headSha);
      } catch {
        jobsRaw = [];
      }
    }

    let passed = 0;
    let failed = 0;
    let pending = 0;
    const checkRuns = [];
    for (const cr of checkRunsRaw) {
      const status = String(cr.status || '');
      const conclusion = cr.conclusion || null;
      if (status !== 'completed') pending++;
      else if (FAILURE_CONCLUSIONS.has(conclusion)) failed++;
      else if (SUCCESS_CONCLUSIONS.has(conclusion)) passed++;
      else pending++;
      checkRuns.push({
        id: cr.id,
        name: cr.name,
        status,
        conclusion,
        details_url: cr.details_url || cr.html_url,
      });
    }

    const failedJobs = [];
    for (const job of jobsRaw) {
      if (FAILURE_CONCLUSIONS.has(job.conclusion)) {
        const jid = Number(job.id);
        failedJobs.push({
          job_id: jid,
          name: String(job.name || `job-${jid}`),
          status: String(job.status || 'completed'),
          conclusion: String(job.conclusion),
          html_url: job.html_url || null,
          failed_steps: (job.steps || [])
            .filter((s) => FAILURE_CONCLUSIONS.has(s.conclusion))
            .map((s) => String(s.name)),
          logs_url: `${repoBaseUrl}/actions/jobs/${jid}/logs`,
        });
      }
    }

    let overall = 'none';
    if (failed > 0 || failedJobs.length > 0) overall = 'failed';
    else if (pending > 0) overall = 'running';
    else if (passed > 0) overall = 'passed';

    const logsUrl = `${repoBaseUrl}/prs/${prNumber}/logs`;
    const hints = {
      passed: 'All CI/CD checks passed. Your PR is ready for maintainer review.',
      failed: `CI/CD failed. Fetch error logs via GET ${logsUrl} , fix locally, and resubmit a patch.`,
      running: 'CI/CD checks are still running. Poll this endpoint again in 15-30 seconds.',
      none: 'No CI/CD check runs reported yet. Wait a few seconds and poll again.',
    };

    return {
      ok: true,
      repo: `${owner}/${repo}`,
      pr_number: prNumber,
      title: String(pr.title || ''),
      state: String(pr.state || ''),
      head_branch: String(head.ref || ''),
      base_branch: String(base.ref || ''),
      head_sha: headSha,
      pr_url: String(pr.html_url || ''),
      mergeable: pr.mergeable ?? null,
      status: overall,
      overall_status: overall,
      passed_count: passed,
      failed_count: failed,
      pending_count: pending,
      check_runs: checkRuns,
      failed_jobs: failedJobs,
      logs_url: logsUrl,
      hint: hints[overall],
    };
  }

  async getPrLogs(owner, repo, prNumber, { tail = 200, includeAll = false } = {}) {
    const pr = await this.getPr(owner, repo, prNumber);
    const headSha = String((pr.head && pr.head.sha) || '');
    if (!headSha) throw new GitHubAPIError(404, `PR #${prNumber} has no head commit SHA`);

    const jobs = await this.getWorkflowJobsForSha(owner, repo, headSha);
    const targetJobs = includeAll ? jobs : jobs.filter((j) => FAILURE_CONCLUSIONS.has(j.conclusion));

    const entries = [];
    for (const job of targetJobs) {
      const jid = Number(job.id);
      const failedSteps = (job.steps || [])
        .filter((s) => FAILURE_CONCLUSIONS.has(s.conclusion))
        .map((s) => String(s.name));
      let logText;
      try {
        logText = await this.getJobLogs(owner, repo, jid, { tail });
      } catch (err) {
        logText = `(unable to fetch log for job ${jid}: ${err.message})`;
      }
      entries.push({
        job_id: jid,
        name: job.name,
        workflow: job._workflow_name,
        status: job.status,
        conclusion: job.conclusion,
        failed_steps: failedSteps,
        log: logText,
      });
    }

    return {
      ok: true,
      repo: `${owner}/${repo}`,
      pr_number: prNumber,
      head_sha: headSha,
      job_count: entries.length,
      jobs: entries,
    };
  }
}

module.exports = { GitHubClient, GitHubAPIError, stripAnsi, tailLines };
