// src/app.js — Zero-dependency Node.js HTTP server & route dispatcher for git-gateway
'use strict';

const crypto = require('crypto');
const http = require('http');
const { ContentStore } = require('./store');
const { GitHubClient, GitHubAPIError } = require('./githubClient');
const { GitPatchEngine, PatchValidationError } = require('./gitOps');
const { handleGitSmartHttp, readBoundedBody } = require('./gitProxy');
const { validatePatchSubmission } = require('./models');
const { renderAgentLanding, renderTodoPage } = require('./promptRenderer');
const { renderAdminHtml } = require('./adminUi');

const VERSION = '3.1.0-node';
const PR_CI_RE = /^prs\/(\d+)\/ci$/;
const PR_LOGS_RE = /^prs\/(\d+)\/logs$/;
const JOB_LOGS_RE = /^actions\/jobs\/(\d+)\/logs$/;

function sendJson(res, status, data) {
  const body = JSON.stringify(data);
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(body),
  });
  res.end(body);
}

function sendText(res, status, text, contentType = 'text/plain; charset=utf-8') {
  const body = String(text ?? '');
  res.writeHead(status, {
    'Content-Type': contentType,
    'Content-Length': Buffer.byteLength(body),
  });
  res.end(body);
}

function safeTokenEqual(provided, expected) {
  if (!provided || !expected) return false;
  const a = Buffer.from(String(provided), 'utf-8');
  const b = Buffer.from(String(expected), 'utf-8');
  if (a.length !== b.length) return false;
  return crypto.timingSafeEqual(a, b);
}

function wantsJson(req, urlObj) {
  const fmt = (urlObj.searchParams.get('format') || '').toLowerCase();
  if (fmt === 'json') return true;
  if (['md', 'markdown', 'text', 'plain'].includes(fmt)) return false;
  const accept = String(req.headers.accept || '').toLowerCase();
  return accept.includes('application/json') && !accept.includes('text/markdown') && !accept.includes('text/plain');
}

function inferBaseUrl(req, settings) {
  if (settings.publicBaseUrl) return settings.publicBaseUrl;
  let proto = 'http';
  let host = req.headers.host || `${settings.host}:${settings.port}`;
  if (settings.trustProxyHeaders) {
    if (req.headers['x-forwarded-proto']) {
      proto = String(req.headers['x-forwarded-proto']).split(',')[0].trim() || proto;
    }
    if (req.headers['x-forwarded-host']) {
      host = String(req.headers['x-forwarded-host']).split(',')[0].trim() || host;
    }
  }
  return `${proto}://${host}`.replace(/\/+$/, '');
}

function parseIntParam(urlObj, name, { defaultVal, min, max }) {
  const raw = urlObj.searchParams.get(name);
  if (raw === null || raw === '') return { ok: true, value: defaultVal };
  if (!/^-?\d+$/.test(raw.trim())) {
    return { ok: false, error: `invalid integer for query param '${name}': '${raw}'` };
  }
  const n = Number.parseInt(raw.trim(), 10);
  return { ok: true, value: Math.max(min, Math.min(max, n)) };
}

function verifyAdmin(req, settings) {
  if (!settings.adminToken) {
    return { ok: false, status: 503, error: 'ADMIN_TOKEN is not configured on the server' };
  }
  const auth = String(req.headers.authorization || '');
  let token = '';
  if (auth.toLowerCase().startsWith('bearer ')) {
    token = auth.slice(7).trim();
  } else if (req.headers['x-admin-token']) {
    token = String(req.headers['x-admin-token']).trim();
  }
  if (!safeTokenEqual(token, settings.adminToken)) {
    return { ok: false, status: 401, error: 'invalid admin token' };
  }
  return { ok: true };
}

async function readContentBody(req, maxBytes = 2 * 1024 * 1024) {
  const raw = await readBoundedBody(req, maxBytes);
  if (raw === null) return { ok: false, status: 413, error: 'body too large' };
  const ctype = String(req.headers['content-type'] || '').toLowerCase();
  const text = raw.toString('utf-8');
  if (ctype.includes('application/json')) {
    try {
      const parsed = JSON.parse(text);
      if (!parsed || typeof parsed.content !== 'string') {
        return { ok: false, status: 400, error: 'JSON body must include string field "content"' };
      }
      return { ok: true, content: parsed.content };
    } catch {
      return { ok: false, status: 400, error: 'invalid JSON body' };
    }
  }
  return { ok: true, content: text };
}

function formatLogsAsText(data) {
  const lines = [
    `# CI/CD Logs — ${data.repo} PR #${data.pr_number} (${String(data.head_sha || '').slice(0, 7)})`,
    '',
  ];
  const jobs = data.jobs || [];
  if (jobs.length === 0) {
    lines.push('(No matching jobs found. Pass ?all=1 to include non-failed jobs.)');
    return lines.join('\n') + '\n';
  }
  for (const job of jobs) {
    lines.push(`=== Job #${job.job_id}: ${job.name} [${job.conclusion || job.status}] ===`);
    if (job.failed_steps && job.failed_steps.length > 0) {
      lines.push(`Failed steps: ${job.failed_steps.join(', ')}`);
    }
    lines.push(job.log || '(empty log)');
    lines.push('');
  }
  return lines.join('\n');
}

function createGatewayHandler(settings) {
  const store = new ContentStore(settings.contentDir);
  const gh = new GitHubClient(settings);
  const gitEngine = new GitPatchEngine(settings);

  async function handlePatchSubmission(req, res, urlObj, owner, repo, repoBaseUrl) {
    const rawBody = await readBoundedBody(req, settings.maxPatchBytes);
    if (rawBody === null) {
      return sendJson(res, 413, { ok: false, error: `patch exceeds limit (${settings.maxPatchBytes} bytes)` });
    }

    const ctype = String(req.headers['content-type'] || '').toLowerCase();
    let sub;
    try {
      if (ctype.includes('application/json')) {
        sub = validatePatchSubmission(JSON.parse(rawBody.toString('utf-8')));
      } else {
        const q = urlObj.searchParams;
        const createPrRaw = (q.get('create_pr') || 'true').toLowerCase();
        sub = validatePatchSubmission({
          nickname: q.get('nickname') || '',
          base_branch: q.get('base_branch') || settings.defaultBaseBranch,
          base_commit: q.get('base_commit') || null,
          message: q.get('message') || req.headers['x-commit-message'] || 'agent patch',
          patch: rawBody.toString('utf-8'),
          pr_title: q.get('pr_title') || null,
          pr_body: q.get('pr_body') || null,
          create_pr: !['0', 'false', 'no'].includes(createPrRaw),
          patch_mode: q.get('patch_mode') || 'auto',
        });
      }
    } catch (err) {
      return sendJson(res, 400, { ok: false, status: 'failed', error: 'invalid submission', detail: err.message });
    }

    let result;
    try {
      result = await gitEngine.applyAndPush(owner, repo, {
        nickname: sub.nickname,
        baseBranch: sub.base_branch,
        baseCommit: sub.base_commit,
        message: sub.message,
        patchText: sub.patch,
        patchMode: sub.patch_mode,
      });
    } catch (err) {
      if (err instanceof PatchValidationError) {
        return sendJson(res, err.statusCode, {
          ok: false,
          status: 'failed',
          repo: `${owner}/${repo}`,
          nickname: sub.nickname,
          base_branch: sub.base_branch,
          error: err.error,
          detail: err.detail,
        });
      }
      return sendJson(res, 500, { ok: false, status: 'failed', error: 'patch apply failed', detail: err.message });
    }

    const resp = {
      ok: true,
      status: result.status,
      repo: `${owner}/${repo}`,
      nickname: sub.nickname,
      base_branch: result.base_branch,
      target_branch: result.target_branch,
      base_commit: result.base_commit,
      commit_sha: result.commit_sha,
      commit_short: result.commit_short,
      message: sub.message,
      pr_number: null,
      pr_url: null,
      pr_reused: false,
      ci_status_url: null,
      ci_logs_url: null,
      error: null,
      detail: null,
      elapsed_ms: result.elapsed_ms,
    };

    if (sub.create_pr) {
      const prTitle = sub.pr_title || `[${sub.nickname}] ${sub.message.split(/\r?\n/)[0].slice(0, 120)}`;
      const prBody =
        sub.pr_body ||
        `Automated CI/CD PR submitted via git-gateway.\n\n- **Contributor nickname**: \`${sub.nickname}\`\n- **Target branch**: \`${result.target_branch}\` → \`${result.base_branch}\`\n- **Base commit**: \`${result.base_commit.slice(0, 12)}\`\n- **Latest commit**: \`${result.commit_short}\`\n`;
      try {
        const pr = await gh.ensurePr(owner, repo, {
          headBranch: result.target_branch,
          baseBranch: result.base_branch,
          title: prTitle,
          body: prBody,
        });
        resp.pr_number = pr.prNumber;
        resp.pr_url = pr.prUrl;
        resp.pr_reused = pr.reused;
        resp.ci_status_url = `${repoBaseUrl}/prs/${pr.prNumber}/ci`;
        resp.ci_logs_url = `${repoBaseUrl}/prs/${pr.prNumber}/logs`;
      } catch (err) {
        resp.detail = `pushed to ${result.target_branch}, but PR creation failed: ${err.message}`;
      }
    }

    return sendJson(res, 200, resp);
  }

  return async (req, res) => {
    try {
      const urlObj = new URL(req.url || '/', `http://${req.headers.host || 'localhost'}`);
      const pathname = decodeURIComponent(urlObj.pathname);
      const method = (req.method || 'GET').toUpperCase();
      const base = inferBaseUrl(req, settings);

      // 1. System endpoints
      if (pathname === '/' && method === 'GET') {
        return sendJson(res, 200, {
          service: 'git-gateway',
          version: VERSION,
          runtime: 'node',
          repo_rules_file: settings.repoRulesFile,
          repo_rules_count: settings.activeRules().length,
          repo_aliases: settings.repoAliases,
          usage: {
            list_repos: `GET ${base}/repos`,
            agent_instructions: `GET ${base}/<repo_path>`,
            todo_list: `GET ${base}/<repo_path>/todo`,
            clone_proxy_readonly: `git clone ${base}/<repo_path>.git`,
            submit_patch: `POST ${base}/<repo_path>/patches`,
            list_prs: `GET ${base}/<repo_path>/prs?nickname=<nick>`,
            pr_ci_status: `GET ${base}/<repo_path>/prs/<pr_number>/ci`,
            pr_ci_error_logs: `GET ${base}/<repo_path>/prs/<pr_number>/logs`,
          },
          admin_endpoints: {
            global_prompt: 'GET/PUT /admin/prompt',
            repo_prompt: 'GET/PUT/DELETE /admin/<repo_path>/prompt',
            repo_todo: 'GET/PUT /admin/<repo_path>/todo',
          },
        });
      }

      if (pathname === '/health' && method === 'GET') {
        return sendJson(res, 200, {
          ok: true,
          version: VERSION,
          runtime: 'node',
          github_token_configured: Boolean(settings.resolveGithubToken()),
          github_token_source: settings.tokenSource(),
          github_token_hint: settings.tokenHint(),
          gh_config_dir: settings.ghConfigDir,
          admin_token_configured: Boolean(settings.adminToken),
          repo_rules_file: settings.repoRulesFile,
          repo_rules_count: settings.activeRules().length,
          repo_aliases: settings.repoAliases,
          temp_dir: settings.tempDir,
          local_storage: 'git worktrees in OS temp dir (deleted per request)',
          content_store: store.info(),
        });
      }

      if (pathname === '/repos' && method === 'GET') {
        const seen = new Map();
        for (const rawRule of settings.activeRules()) {
          const rule = rawRule.trim().replace(/^\/+|\/+$/g, '');
          if (!rule || rule.startsWith('!') || /[*?[]/.test(rule)) continue;
          const parts = rule.split('/');
          if (parts.length !== 2 || !parts[0] || !parts[1]) continue;
          const [owner, repo] = parts;
          if (!settings.isRepoAllowed(owner, repo)) continue;
          const id = `${owner}/${repo}`;
          seen.set(id, {
            id,
            owner,
            repo,
            path: id,
            aliases: [],
            todo_url: `${base}/${id}/todo`,
            prs_url: `${base}/${id}/prs`,
          });
        }
        for (const [alias, target] of Object.entries(settings.repoAliases)) {
          const parts = target.replace(/^\/+|\/+$/g, '').split('/');
          if (parts.length !== 2 || !parts[0] || !parts[1]) continue;
          const [owner, repo] = parts;
          if (!settings.isRepoAllowed(owner, repo)) continue;
          const id = `${owner}/${repo}`;
          if (!seen.has(id)) {
            seen.set(id, {
              id,
              owner,
              repo,
              path: alias,
              aliases: [alias],
              todo_url: `${base}/${alias}/todo`,
              prs_url: `${base}/${alias}/prs`,
            });
          } else {
            const entry = seen.get(id);
            if (!entry.aliases.includes(alias)) entry.aliases.push(alias);
          }
        }
        const repos = Array.from(seen.values()).sort((a, b) => a.id.toLowerCase().localeCompare(b.id.toLowerCase()));
        return sendJson(res, 200, {
          ok: true,
          count: repos.length,
          repos,
          aliases: settings.repoAliases,
          rules_count: settings.activeRules().length,
        });
      }

      // 2. Admin endpoints (HTML UI on GET /admin or /admin/ with Accept: text/html)
      if (
        (pathname === '/admin' || pathname === '/admin/') &&
        method === 'GET' &&
        !wantsJson(req, urlObj) &&
        String(req.headers.accept || '').toLowerCase().includes('text/html')
      ) {
        return sendText(res, 200, renderAdminHtml(), 'text/html; charset=utf-8');
      }

      if (pathname === '/admin/prompt') {
        const auth = verifyAdmin(req, settings);
        if (!auth.ok) return sendJson(res, auth.status, { ok: false, detail: auth.error });
        if (method === 'GET') return sendJson(res, 200, { ok: true, ...store.getGlobalPrompt() });
        if (method === 'PUT') {
          const body = await readContentBody(req);
          if (!body.ok) return sendJson(res, body.status, { ok: false, detail: body.error });
          if (!body.content.trim()) return sendJson(res, 400, { ok: false, detail: 'prompt content cannot be empty' });
          return sendJson(res, 200, { ok: true, ...store.setGlobalPrompt(body.content) });
        }
        return sendJson(res, 405, { ok: false, detail: 'method not allowed' });
      }

      if (pathname.startsWith('/admin/')) {
        const auth = verifyAdmin(req, settings);
        if (!auth.ok) return sendJson(res, auth.status, { ok: false, detail: auth.error });
        const subpath = pathname.slice('/admin/'.length).replace(/^\/+|\/+$/g, '');
        let rawRepo;
        let kind;
        if (subpath.endsWith('/prompt')) {
          rawRepo = subpath.slice(0, -'/prompt'.length);
          kind = 'prompt';
        } else if (subpath.endsWith('/todo')) {
          rawRepo = subpath.slice(0, -'/todo'.length);
          kind = 'todo';
        } else {
          return sendJson(res, 404, { ok: false, detail: 'admin path must end with /prompt or /todo' });
        }
        const resolved = settings.resolveRepoPath(rawRepo);
        if (!resolved) return sendJson(res, 400, { ok: false, detail: `invalid repo path: '${rawRepo}'` });
        const { repoPath, owner, repo } = resolved;
        if (!settings.isRepoAllowed(owner, repo)) {
          return sendJson(res, 403, { ok: false, detail: `repo not allowed: ${owner}/${repo}` });
        }

        if (kind === 'prompt') {
          if (method === 'GET') {
            return sendJson(res, 200, { ok: true, repo: `${owner}/${repo}`, repo_path: repoPath, ...store.getRepoPrompt(owner, repo) });
          }
          if (method === 'DELETE') {
            return sendJson(res, 200, { ok: true, repo: `${owner}/${repo}`, deleted_override: store.deleteRepoPrompt(owner, repo) });
          }
          if (method === 'PUT') {
            const body = await readContentBody(req);
            if (!body.ok) return sendJson(res, body.status, { ok: false, detail: body.error });
            if (!body.content.trim()) return sendJson(res, 400, { ok: false, detail: 'prompt content cannot be empty' });
            return sendJson(res, 200, { ok: true, repo: `${owner}/${repo}`, repo_path: repoPath, ...store.setRepoPrompt(owner, repo, body.content) });
          }
          return sendJson(res, 405, { ok: false, detail: 'method not allowed' });
        }

        // kind === 'todo'
        if (method === 'GET') {
          return sendJson(res, 200, { ok: true, repo: `${owner}/${repo}`, repo_path: repoPath, ...store.getTodo(owner, repo) });
        }
        if (method === 'DELETE') {
          return sendJson(res, 405, { ok: false, detail: 'use PUT with empty/new content to update TODO' });
        }
        if (method === 'PUT') {
          const body = await readContentBody(req);
          if (!body.ok) return sendJson(res, body.status, { ok: false, detail: body.error });
          return sendJson(res, 200, { ok: true, repo: `${owner}/${repo}`, repo_path: repoPath, ...store.setTodo(owner, repo, body.content) });
        }
        return sendJson(res, 405, { ok: false, detail: 'method not allowed' });
      }

      // 3. Unified repository dispatcher
      if (method !== 'GET' && method !== 'POST') {
        return sendJson(res, 405, { ok: false, error: 'method not allowed' });
      }
      const split = settings.splitRepoAndSuffix(pathname);
      if (!split) return sendJson(res, 404, { ok: false, error: 'not found' });

      const { repoPath, owner, repo, suffix } = split;
      if (!settings.isRepoAllowed(owner, repo)) {
        return sendJson(res, 403, { ok: false, error: `repo not allowed: ${owner}/${repo}` });
      }
      const repoBaseUrl = `${base}/${repoPath}`;

      if (suffix === 'info/refs' || suffix === 'git-upload-pack' || suffix === 'git-receive-pack') {
        return await handleGitSmartHttp(req, res, urlObj, settings, owner, repo, repoPath, suffix);
      }

      if (suffix === '' && method === 'GET') {
        const data = renderAgentLanding(settings, store, base, repoPath, owner, repo);
        if (wantsJson(req, urlObj)) return sendJson(res, 200, { ok: true, ...data });
        return sendText(res, 200, data.markdown, 'text/markdown; charset=utf-8');
      }

      if (suffix === 'todo' && method === 'GET') {
        const data = renderTodoPage(settings, store, base, repoPath, owner, repo);
        if (wantsJson(req, urlObj)) return sendJson(res, 200, { ok: true, ...data });
        return sendText(res, 200, data.content, 'text/markdown; charset=utf-8');
      }

      if (suffix === 'patches' && method === 'POST') {
        return await handlePatchSubmission(req, res, urlObj, owner, repo, repoBaseUrl);
      }

      if (suffix === 'prs' && method === 'GET') {
        const lim = parseIntParam(urlObj, 'limit', { defaultVal: 30, min: 1, max: 100 });
        if (!lim.ok) return sendJson(res, 400, { ok: false, detail: lim.error });
        try {
          const prs = await gh.listPrs(owner, repo, {
            state: urlObj.searchParams.get('state') || 'open',
            nickname: urlObj.searchParams.get('nickname') || null,
            baseBranch: urlObj.searchParams.get('base_branch') || null,
            limit: lim.value,
          });
          return sendJson(res, 200, { ok: true, repo: `${owner}/${repo}`, prs });
        } catch (err) {
          return sendJson(res, 502, { ok: false, error: err.message });
        }
      }

      let m = PR_CI_RE.exec(suffix);
      if (m && method === 'GET') {
        const prNumber = Number.parseInt(m[1], 10);
        try {
          const status = await gh.getPrCiStatus(owner, repo, prNumber, { repoBaseUrl });
          return sendJson(res, 200, status);
        } catch (err) {
          const code = err instanceof GitHubAPIError && err.statusCode === 404 ? 404 : 502;
          return sendJson(res, code, { ok: false, error: err.message });
        }
      }

      m = PR_LOGS_RE.exec(suffix);
      if (m && method === 'GET') {
        const prNumber = Number.parseInt(m[1], 10);
        const tail = parseIntParam(urlObj, 'tail', { defaultVal: 200, min: 0, max: 10000 });
        if (!tail.ok) return sendJson(res, 400, { ok: false, detail: tail.error });
        const includeAll = ['1', 'true', 'yes'].includes((urlObj.searchParams.get('all') || '').toLowerCase());
        try {
          const data = await gh.getPrLogs(owner, repo, prNumber, { tail: tail.value, includeAll });
          if (wantsJson(req, urlObj)) return sendJson(res, 200, data);
          return sendText(res, 200, formatLogsAsText(data), 'text/plain; charset=utf-8');
        } catch (err) {
          const code = err instanceof GitHubAPIError && err.statusCode === 404 ? 404 : 502;
          return sendJson(res, code, { ok: false, error: err.message });
        }
      }

      m = JOB_LOGS_RE.exec(suffix);
      if (m && method === 'GET') {
        const jobId = Number.parseInt(m[1], 10);
        const tail = parseIntParam(urlObj, 'tail', { defaultVal: 200, min: 0, max: 10000 });
        if (!tail.ok) return sendJson(res, 400, { ok: false, detail: tail.error });
        try {
          const logText = await gh.getJobLogs(owner, repo, jobId, { tail: tail.value });
          if (wantsJson(req, urlObj)) return sendJson(res, 200, { ok: true, repo: `${owner}/${repo}`, job_id: jobId, log: logText });
          return sendText(res, 200, logText, 'text/plain; charset=utf-8');
        } catch (err) {
          const code = err instanceof GitHubAPIError && err.statusCode === 404 ? 404 : 502;
          return sendJson(res, code, { ok: false, error: err.message });
        }
      }

      return sendJson(res, 404, { ok: false, error: `unknown endpoint: ${pathname}` });
    } catch (err) {
      console.error('[git-gateway] unhandled error:', err);
      if (!res.headersSent) {
        sendJson(res, 500, { ok: false, error: 'internal server error' });
      }
    }
  };
}

function createGatewayServer(settings) {
  const server = http.createServer(createGatewayHandler(settings));
  server.headersTimeout = 15000;
  server.requestTimeout = 600000;
  return server;
}

module.exports = { createGatewayHandler, createGatewayServer, VERSION };
