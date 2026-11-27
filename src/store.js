// src/store.js — Admin prompt & TODO store with atomic JSON persistence + memory fallback
'use strict';

const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const STATE_VERSION = 1;

const DEFAULT_GLOBAL_PROMPT = `# AI Agent Collaboration Guide — \`{{OWNER}}/{{REPO}}\`

You are collaborating on **{{OWNER}}/{{REPO}}** through a zero-permission Git patch gateway (\`git-gateway\`).
You do **NOT** need a GitHub account, SSH key, or Personal Access Token.

## Step 1 — Ask the User for Their Nickname (MANDATORY)
Before doing any code changes or submissions, ask the user to choose a short **nickname**
(letters, digits, \`-\`, \`_\`, 1–32 chars, e.g. \`alice\` or \`dev-bob\`).
All of your submitted changes will be pushed to the isolated branch:
\`<nickname>/<original_branch>\` (for example, \`alice/main\`), and a CI/CD Pull Request will be opened against \`<original_branch>\`.

## Step 2 — Read the TODO List
Fetch the latest task list from the TODO endpoint:
- **TODO Endpoint**: \`GET {{TODO_URL}}\`

## Step 3 — Clone the Repository (with Proxy Fallback)
1. First, try cloning directly from GitHub:
   \`\`\`bash
   git clone {{GITHUB_URL}}
   \`\`\`
2. If GitHub is unreachable, slow, or private, clone via this gateway's **read-only clone proxy** (zero credentials needed):
   \`\`\`bash
   git clone {{CLONE_PROXY_URL}}
   \`\`\`
> Note: Direct \`git push\` to the proxy is disabled (403). All contributions MUST be submitted via the Patch API below.

## Step 4 — Make Changes & Generate a Patch
Generate a patch via \`git diff > changes.patch\` or \`git format-patch --stdout --base=<base_sha> <base_branch>..HEAD > changes.patch\`.

## Step 5 — Submit Patch (Auto-Push & Open CI/CD PR)
\`\`\`bash
curl -sS -X POST "{{PATCH_URL}}?nickname=<NICKNAME>&base_branch={{DEFAULT_BASE_BRANCH}}&message=feat:+describe+change" \\
  --data-binary @changes.patch
\`\`\`

## Step 6 — Check CI/CD Status & Read Error Logs
\`\`\`bash
curl -sS "{{PRS_URL}}/<PR_NUMBER>/ci"
curl -sS "{{PRS_URL}}/<PR_NUMBER>/logs"
\`\`\`
`;

const DEFAULT_TODO_TEMPLATE = `# TODO List — \`{{OWNER}}/{{REPO}}\`

*(Admin has not configured a TODO list for this repository yet. Ask the maintainer to update it via \`PUT /admin/{{REPO_PATH}}/todo\`.)*
`;

function emptyState() {
  return {
    version: STATE_VERSION,
    global_prompt: null,
    repo_prompts: {},
    repo_todos: {},
  };
}

class ContentStore {
  constructor(contentDir = null) {
    this._dir = contentDir ? path.resolve(contentDir) : null;
    this._statePath = this._dir ? path.join(this._dir, 'content.json') : null;
    this._persistent = false;
    this._degradedReason = null;
    this._lastError = null;
    this._state = emptyState();
    this._sig = null;

    if (this._dir) {
      this._initPersistence();
    }
    if (!this._persistent) {
      console.warn(
        `[git-gateway] content persistence DISABLED (${this._degradedReason || 'CONTENT_DIR not configured'}) — Admin prompts/TODOs will be lost on restart`
      );
    }
  }

  _initPersistence() {
    try {
      fs.mkdirSync(this._dir, { recursive: true });
      const probe = path.join(this._dir, `.write-probe-${process.pid}-${Date.now()}`);
      fs.writeFileSync(probe, 'ok', 'utf-8');
      fs.unlinkSync(probe);
    } catch (err) {
      this._degradedReason = `${this._dir} is not writable (${err.code || err.name}: ${err.message})`;
      return;
    }

    this._persistent = true;
    try {
      this._reload(true);
    } catch (err) {
      this._degradedReason = `existing content.json could not be loaded: ${err.message}`;
      this._quarantine();
    }
  }

  _quarantine() {
    if (!this._statePath || !fs.existsSync(this._statePath)) return;
    const stamp = new Date().toISOString().replace(/[-:]/g, '').replace(/\.\d+Z$/, 'Z');
    const target = path.join(this._dir, `content.corrupt-${stamp}.json`);
    try {
      fs.renameSync(this._statePath, target);
      console.error(`[git-gateway] corrupt content.json quarantined to ${target}`);
    } catch {
      // ignore
    }
    this._state = emptyState();
    this._sig = null;
  }

  _signature() {
    if (!this._statePath) return null;
    try {
      const st = fs.statSync(this._statePath);
      return `${st.mtimeMs}:${st.size}`;
    } catch {
      return null;
    }
  }

  _reload(force = false) {
    if (!this._persistent) return;
    const sig = this._signature();
    if (!force && sig === this._sig) return;
    this._sig = sig;
    if (!sig) {
      this._state = emptyState();
      return;
    }
    const raw = JSON.parse(fs.readFileSync(this._statePath, 'utf-8'));
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
      throw new Error('content store root must be a JSON object');
    }
    const state = emptyState();
    if (raw.global_prompt && typeof raw.global_prompt.content === 'string') {
      state.global_prompt = raw.global_prompt;
    }
    for (const key of ['repo_prompts', 'repo_todos']) {
      if (raw[key] && typeof raw[key] === 'object') {
        for (const [k, v] of Object.entries(raw[key])) {
          if (v && typeof v === 'object' && typeof v.content === 'string') {
            state[key][k] = v;
          }
        }
      }
    }
    this._state = state;
  }

  _writeAtomic() {
    if (!this._persistent || !this._statePath) return;
    const tmp = path.join(this._dir, `.content-${crypto.randomBytes(6).toString('hex')}.json`);
    try {
      fs.writeFileSync(tmp, JSON.stringify(this._state, null, 2), 'utf-8');
      fs.renameSync(tmp, this._statePath);
      this._sig = this._signature();
    } catch (err) {
      try { fs.unlinkSync(tmp); } catch { /* ignore */ }
      this._lastError = `${err.code || err.name}: ${err.message}`;
      throw err;
    }
  }

  _mutate(fn) {
    if (this._persistent) this._reload(true);
    fn(this._state);
    this._writeAtomic();
  }

  _snapshot() {
    if (this._persistent) {
      try { this._reload(false); } catch { /* ignore */ }
    }
    return this._state;
  }

  info() {
    const st = this._snapshot();
    return {
      mode: this._persistent ? 'file' : 'memory',
      path: this._persistent ? this._statePath : null,
      degraded_reason: this._degradedReason,
      last_error: this._lastError,
      global_prompt_customized: Boolean(st.global_prompt),
      repo_prompt_count: Object.keys(st.repo_prompts || {}).length,
      repo_todo_count: Object.keys(st.repo_todos || {}).length,
    };
  }

  getGlobalPrompt() {
    const entry = this._snapshot().global_prompt;
    if (entry) return { ...entry };
    return { content: DEFAULT_GLOBAL_PROMPT, updated_at: null, is_default: true };
  }

  getRepoPrompt(owner, repo) {
    const st = this._snapshot();
    const key = `${owner}/${repo}`;
    if (st.repo_prompts[key]) return { ...st.repo_prompts[key], scope: 'repo' };
    if (st.global_prompt) return { ...st.global_prompt, scope: 'global' };
    return { content: DEFAULT_GLOBAL_PROMPT, updated_at: null, is_default: true, scope: 'builtin_default' };
  }

  getTodo(owner, repo) {
    const st = this._snapshot();
    const key = `${owner}/${repo}`;
    if (st.repo_todos[key]) return { ...st.repo_todos[key] };
    return { content: DEFAULT_TODO_TEMPLATE, updated_at: null, is_default: true };
  }

  setGlobalPrompt(content) {
    const entry = { content, updated_at: new Date().toISOString(), is_default: false };
    this._mutate((st) => { st.global_prompt = { ...entry }; });
    return entry;
  }

  setRepoPrompt(owner, repo, content) {
    const key = `${owner}/${repo}`;
    const entry = { content, updated_at: new Date().toISOString(), is_default: false, scope: 'repo' };
    this._mutate((st) => { st.repo_prompts[key] = { ...entry }; });
    return entry;
  }

  deleteRepoPrompt(owner, repo) {
    const key = `${owner}/${repo}`;
    let removed = false;
    this._mutate((st) => {
      if (Object.prototype.hasOwnProperty.call(st.repo_prompts, key)) {
        delete st.repo_prompts[key];
        removed = true;
      }
    });
    return removed;
  }

  setTodo(owner, repo, content) {
    const key = `${owner}/${repo}`;
    const entry = { content, updated_at: new Date().toISOString(), is_default: false };
    this._mutate((st) => { st.repo_todos[key] = { ...entry }; });
    return entry;
  }
}

module.exports = { ContentStore, DEFAULT_GLOBAL_PROMPT, DEFAULT_TODO_TEMPLATE };
