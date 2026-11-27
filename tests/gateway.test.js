'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const fsp = fs.promises;
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');

const { Settings } = require('../src/config');
const { createGatewayServer } = require('../src/app');
const { decodeGitCQuoted, extractTouchedPaths } = require('../src/gitOps');

function listenServer(server) {
  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => {
      const addr = server.address();
      resolve(`http://127.0.0.1:${addr.port}`);
    });
  });
}

function closeServer(server) {
  return new Promise((resolve) => server.close(() => resolve()));
}

test('Node git-gateway end-to-end: /repos, admin persistence, clone proxy guardrails, patch push, CI/logs, CI-poisoning & symlink block', async () => {
  const tmp = await fsp.mkdtemp(path.join(os.tmpdir(), 'ggw-node-test-'));
  const bareRepo = path.join(tmp, 'upstream', 'alice', 'demo.git');
  const seedWork = path.join(tmp, 'seed');
  const contentDir = path.join(tmp, 'content');
  const rulesFile = path.join(tmp, 'repos.txt');

  // 1. Create real local bare repo with initial commit on main
  fs.mkdirSync(bareRepo, { recursive: true });
  execFileSync('git', ['init', '--bare', bareRepo]);
  execFileSync('git', ['init', seedWork]);
  execFileSync('git', ['config', 'user.name', 'Seed'], { cwd: seedWork });
  execFileSync('git', ['config', 'user.email', 'seed@example.com'], { cwd: seedWork });
  fs.writeFileSync(path.join(seedWork, 'hello.txt'), 'hello v1\n');
  execFileSync('git', ['add', '.'], { cwd: seedWork });
  execFileSync('git', ['commit', '-m', 'init'], { cwd: seedWork });
  execFileSync('git', ['branch', '-M', 'main'], { cwd: seedWork });
  execFileSync('git', ['remote', 'add', 'origin', bareRepo], { cwd: seedWork });
  execFileSync('git', ['push', 'origin', 'main'], { cwd: seedWork });
  const baseSha = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: seedWork, encoding: 'utf-8' }).trim();

  fs.writeFileSync(rulesFile, 'alice/demo\nalice/secret\n!alice/secret\n');

  // 2. Local Mock GitHub API server (PRs, check-runs, workflow jobs, 302 redirected logs)
  const prs = [];
  let seenAuthOnBlob = false;
  const mockGh = http.createServer((req, res) => {
    const u = new URL(req.url, 'http://127.0.0.1');
    if (u.pathname === '/blob/job-101.log') {
      if (req.headers.authorization) seenAuthOnBlob = true;
      res.writeHead(200, { 'Content-Type': 'text/plain' });
      res.end('\x1B[31mERROR: boom\x1B[0m\nline2\n');
      return;
    }
    if (u.pathname === '/repos/alice/demo/pulls' && req.method === 'GET') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(prs));
      return;
    }
    if (u.pathname === '/repos/alice/demo/pulls' && req.method === 'POST') {
      let body = '';
      req.on('data', (c) => (body += c));
      req.on('end', () => {
        const data = JSON.parse(body);
        const pr = {
          number: prs.length + 1,
          title: data.title,
          state: 'open',
          head: { ref: data.head, sha: 'deadbeef1234567' },
          base: { ref: data.base },
          html_url: `https://github.com/alice/demo/pull/${prs.length + 1}`,
        };
        prs.push(pr);
        res.writeHead(201, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify(pr));
      });
      return;
    }
    if (u.pathname === '/repos/alice/demo/pulls/1' && req.method === 'GET') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(prs[0]));
      return;
    }
    if (u.pathname.endsWith('/check-runs')) {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ check_runs: [{ id: 1, name: 'ci', status: 'completed', conclusion: 'failure' }] }));
      return;
    }
    if (u.pathname.endsWith('/actions/runs')) {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ workflow_runs: [{ id: 55, workflow_id: 1, name: 'CI', run_number: 1, run_attempt: 1 }] }));
      return;
    }
    if (u.pathname.endsWith('/actions/runs/55/jobs')) {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ jobs: [{ id: 101, name: 'test', status: 'completed', conclusion: 'failure', steps: [{ name: 'Run tests', conclusion: 'failure' }] }] }));
      return;
    }
    if (u.pathname === '/repos/alice/demo/actions/jobs/101/logs') {
      res.writeHead(302, { Location: `${mockGhUrl}/blob/job-101.log` });
      res.end();
      return;
    }
    res.writeHead(404);
    res.end('{}');
  });

  const mockGhUrl = await listenServer(mockGh);

  const settings = new Settings({
    githubWebBase: `file://${path.join(tmp, 'upstream')}`,
    githubApiBase: mockGhUrl,
    githubToken: 'ghp_test_token',
    adminToken: 'admin-secret',
    contentDir,
    tempDir: path.join(tmp, 'temp'),
    repoRulesFile: rulesFile,
    repoAliases: { 'p/demo': 'alice/demo' },
    useGhCli: false,
  });

  const gwServer = createGatewayServer(settings);
  const gwUrl = await listenServer(gwServer);

  try {
    // A. /health & /repos
    const health = await (await fetch(`${gwUrl}/health`)).json();
    assert.equal(health.ok, true);
    assert.equal(health.runtime, 'node');
    assert.equal(health.content_store.mode, 'file');

    const reposRes = await (await fetch(`${gwUrl}/repos`)).json();
    assert.equal(reposRes.count, 1);
    assert.equal(reposRes.repos[0].id, 'alice/demo');
    assert.deepEqual(reposRes.repos[0].aliases, ['p/demo']);

    // Explicitly excluded repo (!alice/secret) must reject direct requests with 403
    const secretTodo = await fetch(`${gwUrl}/alice/secret/todo`);
    assert.equal(secretTodo.status, 403);
    const secretPatch = await fetch(`${gwUrl}/alice/secret/patches`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ nickname: 'bot1', base_branch: 'main', message: 'nope', patch: 'diff --git a/a b/a\n' }),
    });
    assert.equal(secretPatch.status, 403);

    // Admin HTML UI serves on Accept: text/html without leaking token, while JSON API stays 401
    const adminHtml = await fetch(`${gwUrl}/admin/`, { headers: { Accept: 'text/html' } });
    assert.equal(adminHtml.status, 200);
    assert.match(adminHtml.headers.get('content-type') || '', /text\/html/);
    const adminHtmlBody = await adminHtml.text();
    assert.match(adminHtmlBody, /git-gateway/);
    assert.equal(adminHtmlBody.includes('admin-secret'), false);
    const adminApiCheck = await fetch(`${gwUrl}/admin/`, { headers: { Accept: 'application/json' } });
    assert.equal(adminApiCheck.status, 401);

    // B. Admin auth & persistence
    const unauth = await fetch(`${gwUrl}/admin/alice/demo/todo`, { method: 'PUT', body: '- task 1' });
    assert.equal(unauth.status, 401);

    const putTodo = await fetch(`${gwUrl}/admin/alice/demo/todo`, {
      method: 'PUT',
      headers: { Authorization: 'Bearer admin-secret' },
      body: '- task 1\n- task 2',
    });
    assert.equal(putTodo.status, 200);

    const todoMd = await (await fetch(`${gwUrl}/p/demo/todo`)).text();
    assert.match(todoMd, /task 1/);

    // C. Read-only clone proxy blocks git-receive-pack & HPP
    const pushBlock = await fetch(`${gwUrl}/alice/demo.git/git-receive-pack`, { method: 'POST' });
    assert.equal(pushBlock.status, 403);
    const hppBlock = await fetch(`${gwUrl}/alice/demo.git/info/refs?service=git-receive-pack&service=git-upload-pack`);
    assert.equal(hppBlock.status, 403);

    // D. CI poisoning (including octal-escaped C-quoted path) & symlink blocked with 403
    assert.equal(decodeGitCQuoted('".\\147ithub/workflows/ci.yml"'), '.github/workflows/ci.yml');
    const evilPatch = [
      'diff --git "a/.\\147ithub/workflows/ci.yml" "b/.\\147ithub/workflows/ci.yml"',
      'new file mode 100644',
      '--- /dev/null',
      '+++ "b/.\\147ithub/workflows/ci.yml"',
      '@@ -0,0 +1 @@',
      '+name: pwn',
      '',
    ].join('\n');
    assert.ok(extractTouchedPaths(evilPatch).includes('.github/workflows/ci.yml'));
    const evilRes = await fetch(`${gwUrl}/alice/demo/patches?nickname=bot1&base_branch=main`, {
      method: 'POST',
      body: evilPatch,
    });
    assert.equal(evilRes.status, 403);

    const symPatch = [
      'diff --git a/link.txt b/link.txt',
      'new file mode 120000',
      '--- /dev/null',
      '+++ b/link.txt',
      '@@ -0,0 +1 @@',
      '+/etc/passwd',
      '\\ No newline at end of file',
      '',
    ].join('\n');
    const symRes = await fetch(`${gwUrl}/alice/demo/patches?nickname=bot1&base_branch=main`, {
      method: 'POST',
      body: symPatch,
    });
    assert.equal(symRes.status, 403);

    // E. Valid patch -> push to bot1/main -> PR #1 created -> CI status & sanitized logs without token leak
    fs.writeFileSync(path.join(seedWork, 'hello.txt'), 'hello v2\n');
    const goodPatch = execFileSync('git', ['diff'], { cwd: seedWork, encoding: 'utf-8' });
    const pushRes = await fetch(`${gwUrl}/alice/demo/patches?nickname=bot1&base_branch=main&base_commit=${baseSha}&message=feat+v2`, {
      method: 'POST',
      body: goodPatch,
    });
    assert.equal(pushRes.status, 200);
    const pushJson = await pushRes.json();
    assert.equal(pushJson.status, 'pushed');
    assert.equal(pushJson.target_branch, 'bot1/main');
    assert.equal(pushJson.pr_number, 1);

    // Verify branch actually pushed to bare repo
    const pushedSha = execFileSync('git', ['rev-parse', 'refs/heads/bot1/main'], { cwd: bareRepo, encoding: 'utf-8' }).trim();
    assert.equal(pushedSha, pushJson.commit_sha);

    // Verify CI status & ANSI-stripped logs + NO Authorization forwarded on 302 redirect
    const ciJson = await (await fetch(`${gwUrl}/alice/demo/prs/1/ci`)).json();
    assert.equal(ciJson.overall_status, 'failed');
    assert.equal(ciJson.failed_jobs.length, 1);

    const logsTxt = await (await fetch(`${gwUrl}/alice/demo/prs/1/logs`)).text();
    assert.match(logsTxt, /ERROR: boom/);
    assert.ok(!logsTxt.includes('\x1B['), 'ANSI escapes must be stripped');
    assert.equal(seenAuthOnBlob, false, 'Authorization must never leak on 302 redirect');
  } finally {
    await closeServer(gwServer);
    await closeServer(mockGh);
    await fsp.rm(tmp, { recursive: true, force: true });
  }
});

test('Settings resolves GitHub token from host gh hosts.yml without gh binary, falls back to env, and emits hint when missing', async () => {
  const tmpGh = await fsp.mkdtemp(path.join(os.tmpdir(), 'ggw-gh-yml-'));
  const prevGh = process.env.GH_TOKEN;
  const prevGithub = process.env.GITHUB_TOKEN;
  delete process.env.GH_TOKEN;
  delete process.env.GITHUB_TOKEN;

  try {
    fs.writeFileSync(
      path.join(tmpGh, 'hosts.yml'),
      'github.com:\n    oauth_token: gho_borrowed_from_hosts_yml\n    user: tester\n',
      'utf-8'
    );
    const s1 = new Settings({ ghConfigDir: tmpGh, ghCliPath: 'nonexistent-gh-bin-xyz', useGhCli: true });
    assert.equal(s1.resolveGithubToken(), 'gho_borrowed_from_hosts_yml');
    assert.equal(s1.tokenSource(), 'gh_hosts_yml');
    assert.equal(s1.tokenHint(), null);

    // Missing hosts.yml + missing gh CLI -> falls back to env token
    process.env.GH_TOKEN = 'ghp_env_fallback_token';
    const emptyGh = path.join(tmpGh, 'empty');
    fs.mkdirSync(emptyGh);
    const s2 = new Settings({ ghConfigDir: emptyGh, ghCliPath: 'nonexistent-gh-bin-xyz', useGhCli: true });
    assert.equal(s2.resolveGithubToken(), 'ghp_env_fallback_token');
    assert.equal(s2.tokenSource(), 'env/arg');

    // Missing both -> clear hint
    delete process.env.GH_TOKEN;
    const s3 = new Settings({ ghConfigDir: emptyGh, ghCliPath: 'nonexistent-gh-bin-xyz', useGhCli: true });
    assert.equal(s3.resolveGithubToken(), '');
    assert.equal(s3.tokenSource(), 'none');
    assert.match(s3.tokenHint() || '', /GH_HOST_CONFIG_DIR|GH_TOKEN/);
  } finally {
    if (prevGh === undefined) delete process.env.GH_TOKEN; else process.env.GH_TOKEN = prevGh;
    if (prevGithub === undefined) delete process.env.GITHUB_TOKEN; else process.env.GITHUB_TOKEN = prevGithub;
    await fsp.rm(tmpGh, { recursive: true, force: true });
  }
});
