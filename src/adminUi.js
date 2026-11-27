// src/adminUi.js — Lightweight self-contained Developer/Admin UI for git-gateway (/admin/)
'use strict';

function renderAdminHtml() {
  return `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>git-gateway Admin Console</title>
  <style>
    :root { --bg:#f8fafc; --card:#ffffff; --bd:#cbd5e1; --fg:#0f172a; --muted:#475569; --accent:#059669; --accent-d:#047857; --danger:#dc2626; }
    * { box-sizing:border-box; }
    body { margin:0; font-family:ui-sans-serif,system-ui,-apple-system,sans-serif; background:var(--bg); color:var(--fg); }
    header { display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between; gap:12px; padding:12px 20px; background:#0f172a; color:#fff; }
    header h1 { margin:0; font-size:16px; font-weight:700; }
    header nav { display:flex; align-items:center; gap:10px; font-size:12px; }
    header nav a { color:#cbd5e1; text-decoration:none; padding:5px 10px; border:1px solid #334155; border-radius:6px; }
    header nav a:hover { background:#1e293b; color:#fff; }
    main { max-width:1180px; margin:18px auto; padding:0 16px 32px; display:grid; gap:16px; }
    .card { background:var(--card); border:1px solid var(--bd); border-radius:10px; padding:16px; }
    .row { display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
    .grid2 { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
    @media (max-width:860px) { .grid2 { grid-template-columns:1fr; } }
    input, select, textarea { width:100%; padding:8px 10px; border:1px solid var(--bd); border-radius:7px; font-size:13px; font-family:inherit; background:#fff; }
    textarea { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px; min-height:190px; line-height:1.5; }
    button { border:0; border-radius:7px; padding:8px 14px; font-size:13px; font-weight:600; cursor:pointer; background:var(--accent); color:#fff; }
    button:hover { background:var(--accent-d); }
    button.sec { background:#e2e8f0; color:#0f172a; }
    button.sec:hover { background:#cbd5e1; }
    button.del { background:var(--danger); }
    .muted { font-size:12px; color:var(--muted); }
    .pill { display:inline-block; padding:3px 8px; border-radius:99px; font-size:11px; font-weight:700; background:#d1fae5; color:#065f46; }
    .warn { background:#fef3c7; color:#92400e; padding:8px 12px; border-radius:7px; font-size:12px; margin-top:8px; display:none; }
    .status { font-size:12px; margin-top:8px; min-height:18px; }
  </style>
</head>
<body>
  <header>
    <div class="row">
      <h1>git-gateway 管理员控制台</h1>
      <span class="pill" id="authBadge">未连接</span>
    </div>
    <nav>
      <a href="/manage/">前往 elyfn 开发者后台</a>
      <a href="/">返回 Atlas 用户画布</a>
    </nav>
  </header>
  <main>
    <section class="card">
      <div class="row">
        <div style="flex:1;min-width:240px;">
          <input id="tokenInput" type="password" placeholder="输入 GIT_GATEWAY_ADMIN_TOKEN（仅保存在当前会话 sessionStorage）" />
        </div>
        <button id="connectBtn" type="button">连接并加载</button>
        <button id="logoutBtn" type="button" class="sec">清除 Token</button>
      </div>
      <div id="healthInfo" class="muted" style="margin-top:8px;">正在读取网关健康状态…</div>
      <div id="tokenWarn" class="warn"></div>
    </section>

    <section class="card">
      <div class="row" style="justify-content:space-between;margin-bottom:10px;">
        <strong>全局 Agent Prompt (/admin/prompt)</strong>
        <button id="saveGlobalBtn" type="button">保存全局 Prompt</button>
      </div>
      <textarea id="globalPrompt" placeholder="填写全局 AI Agent 引导规则…"></textarea>
      <div id="globalStatus" class="status muted"></div>
    </section>

    <section class="card">
      <div class="row" style="justify-content:space-between;margin-bottom:12px;">
        <strong>分仓 Prompt &amp; TODO 管理（白名单仓库）</strong>
        <div style="min-width:280px;flex:0 1 360px;">
          <select id="repoSelect"><option value="">请先连接 Admin Token…</option></select>
        </div>
      </div>
      <div class="grid2">
        <div>
          <div class="row" style="justify-content:space-between;margin-bottom:6px;">
            <span class="muted"><strong>仓库 Prompt</strong> (<span id="repoPromptScope">-</span>)</span>
            <div class="row">
              <button id="saveRepoPromptBtn" type="button">保存覆盖</button>
              <button id="delRepoPromptBtn" type="button" class="del">恢复默认/删除覆盖</button>
            </div>
          </div>
          <textarea id="repoPrompt" placeholder="为当前仓库设置专属 Prompt…"></textarea>
        </div>
        <div>
          <div class="row" style="justify-content:space-between;margin-bottom:6px;">
            <span class="muted"><strong>仓库 TODO Markdown</strong></span>
            <button id="saveRepoTodoBtn" type="button">保存 TODO</button>
          </div>
          <textarea id="repoTodo" placeholder="- [ ] 待办任务…"></textarea>
        </div>
      </div>
      <div id="repoStatus" class="status muted"></div>
    </section>
  </main>
  <script>
    const KEY_STORE = 'ggw_admin_token';
    const $ = (id) => document.getElementById(id);
    const getToken = () => sessionStorage.getItem(KEY_STORE) || '';
    const authHeaders = () => ({ Authorization: 'Bearer ' + getToken() });

    async function api(path, opts = {}) {
      const res = await fetch(path, { ...opts, headers: { ...authHeaders(), ...(opts.headers || {}) } });
      const data = await res.json().catch(() => ({ ok: false, detail: 'HTTP ' + res.status }));
      if (!res.ok) throw new Error(data.detail || data.error || ('HTTP ' + res.status));
      return data;
    }

    async function loadHealthAndRepos() {
      const h = await fetch('/health').then((r) => r.json()).catch(() => null);
      if (h) {
        $('healthInfo').textContent = '版本 ' + h.version + ' · 规则数 ' + h.repo_rules_count + ' · 凭据来源 ' + h.github_token_source + ' · 存储 ' + (h.content_store?.mode || '-');
        if (h.github_token_hint) {
          $('tokenWarn').style.display = 'block';
          $('tokenWarn').textContent = h.github_token_hint;
        } else {
          $('tokenWarn').style.display = 'none';
        }
      }
      const repos = await fetch('/repos').then((r) => r.json()).catch(() => ({ repos: [] }));
      const sel = $('repoSelect');
      sel.innerHTML = '';
      for (const r of (repos.repos || [])) {
        const opt = document.createElement('option');
        opt.value = r.path || r.id;
        opt.textContent = r.id;
        sel.appendChild(opt);
      }
    }

    async function loadRepoData() {
      const repo = $('repoSelect').value;
      if (!repo || !getToken()) return;
      $('repoStatus').textContent = '加载仓库配置中…';
      try {
        const [p, t] = await Promise.all([
          api('/admin/' + repo + '/prompt'),
          api('/admin/' + repo + '/todo'),
        ]);
        $('repoPrompt').value = p.content || '';
        $('repoPromptScope').textContent = '当前作用域: ' + (p.scope || '-');
        $('repoTodo').value = t.content || '';
        $('repoStatus').textContent = '已加载 ' + repo;
      } catch (e) {
        $('repoStatus').textContent = '加载失败：' + e.message;
      }
    }

    async function connectAll() {
      if (!getToken()) { $('authBadge').textContent = '未连接'; return; }
      try {
        const g = await api('/admin/prompt');
        $('globalPrompt').value = g.content || '';
        $('authBadge').textContent = '已认证';
        $('globalStatus').textContent = '全局 Prompt 已同步';
        await loadRepoData();
      } catch (e) {
        $('authBadge').textContent = 'Token 无效';
        $('globalStatus').textContent = '鉴权失败：' + e.message;
      }
    }

    $('connectBtn').onclick = () => {
      sessionStorage.setItem(KEY_STORE, $('tokenInput').value.trim());
      connectAll();
    };
    $('logoutBtn').onclick = () => {
      sessionStorage.removeItem(KEY_STORE);
      $('tokenInput').value = '';
      $('authBadge').textContent = '未连接';
    };
    $('saveGlobalBtn').onclick = async () => {
      try {
        await api('/admin/prompt', { method: 'PUT', headers: { 'Content-Type': 'text/plain;charset=utf-8' }, body: $('globalPrompt').value });
        $('globalStatus').textContent = '全局 Prompt 已保存';
      } catch (e) { $('globalStatus').textContent = '保存失败：' + e.message; }
    };
    $('repoSelect').onchange = loadRepoData;
    $('saveRepoPromptBtn').onclick = async () => {
      const repo = $('repoSelect').value; if (!repo) return;
      try {
        await api('/admin/' + repo + '/prompt', { method: 'PUT', headers: { 'Content-Type': 'text/plain;charset=utf-8' }, body: $('repoPrompt').value });
        $('repoStatus').textContent = '已保存分仓 Prompt';
        loadRepoData();
      } catch (e) { $('repoStatus').textContent = '失败：' + e.message; }
    };
    $('delRepoPromptBtn').onclick = async () => {
      const repo = $('repoSelect').value; if (!repo) return;
      try {
        await api('/admin/' + repo + '/prompt', { method: 'DELETE' });
        $('repoStatus').textContent = '已删除分仓 Prompt 覆盖，回退全局';
        loadRepoData();
      } catch (e) { $('repoStatus').textContent = '失败：' + e.message; }
    };
    $('saveRepoTodoBtn').onclick = async () => {
      const repo = $('repoSelect').value; if (!repo) return;
      try {
        await api('/admin/' + repo + '/todo', { method: 'PUT', headers: { 'Content-Type': 'text/plain;charset=utf-8' }, body: $('repoTodo').value });
        $('repoStatus').textContent = '已保存 TODO';
      } catch (e) { $('repoStatus').textContent = '失败：' + e.message; }
    };

    (async () => {
      $('tokenInput').value = getToken();
      await loadHealthAndRepos();
      if (getToken()) await connectAll();
    })();
  </script>
</body>
</html>`;
}

module.exports = { renderAdminHtml };
