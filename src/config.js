// src/config.js — Zero-dependency configuration, gitignore-style repo rules, and token resolution
'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const posix = path.posix;
const { execFileSync } = require('child_process');

const DEFAULT_BLOCKED_PATHS = [
  '.github/workflows/*',
  '.github/workflows/**',
  '.github/actions/*',
  '.github/actions/**',
];

function parseBoolEnv(name, defaultVal = false) {
  const raw = process.env[name];
  if (raw === undefined) return defaultVal;
  return ['1', 'true', 'yes', 'on'].includes(String(raw).trim().toLowerCase());
}

function parseIntEnv(name, defaultVal) {
  const raw = process.env[name];
  if (raw === undefined || raw === '') return defaultVal;
  const n = Number.parseInt(raw, 10);
  return Number.isFinite(n) ? n : defaultVal;
}

/** Convert a glob/fnmatch pattern (with * ? ** [abc]) to a RegExp. */
function globToRegExp(pattern) {
  let re = '^';
  let i = 0;
  while (i < pattern.length) {
    const ch = pattern[i];
    if (ch === '*') {
      if (pattern[i + 1] === '*') {
        re += '.*';
        i += 2;
      } else {
        re += '[^/]*';
        i += 1;
      }
    } else if (ch === '?') {
      re += '[^/]';
      i += 1;
    } else if (ch === '[') {
      const close = pattern.indexOf(']', i + 1);
      if (close > i + 1) {
        let cls = pattern.slice(i + 1, close);
        if (cls.startsWith('!')) cls = '^' + cls.slice(1);
        re += '[' + cls + ']';
        i = close + 1;
      } else {
        re += '\\[';
        i += 1;
      }
    } else if ('\\.^$+(){}|'.includes(ch)) {
      re += '\\' + ch;
      i += 1;
    } else {
      re += ch;
      i += 1;
    }
  }
  re += '$';
  return new RegExp(re);
}

function fnmatchCase(str, pattern) {
  // Match Python fnmatch.fnmatchcase semantics where * matches across / too
  let re = '^';
  let i = 0;
  while (i < pattern.length) {
    const ch = pattern[i];
    if (ch === '*') {
      re += '.*';
    } else if (ch === '?') {
      re += '.';
    } else if (ch === '[') {
      const close = pattern.indexOf(']', i + 1);
      if (close > i + 1) {
        let cls = pattern.slice(i + 1, close);
        if (cls.startsWith('!')) cls = '^' + cls.slice(1);
        re += '[' + cls + ']';
        i = close + 1;
        continue;
      }
      re += '\\[';
    } else if ('\\.^$+(){}|'.includes(ch)) {
      re += '\\' + ch;
    } else {
      re += ch;
    }
    i += 1;
  }
  re += '$';
  return new RegExp(re).test(str);
}

function parseRepoRuleLine(line) {
  let s = line.trim();
  if (!s || s.startsWith('#')) return null;
  let negated = false;
  if (s.startsWith('!')) {
    negated = true;
    s = s.slice(1).trim();
  }
  if (!s) return null;
  if (s.endsWith('/')) s += '*';
  s = s.replace(/^\/+/, '');
  return { pattern: s, negated, raw: line.trim() };
}

class RepoRuleMatcher {
  constructor({ rulesFile = null, inlineRules = null } = {}) {
    this.rulesFile = rulesFile ? path.resolve(rulesFile) : null;
    this._inlineRules = Array.isArray(inlineRules) ? [...inlineRules] : null;
    this._sig = null;
    this._rules = [];
    this._load();
  }

  _load() {
    if (this._inlineRules !== null) {
      this._rules = this._inlineRules.map(parseRepoRuleLine).filter(Boolean);
      return;
    }
    if (!this.rulesFile) {
      this._rules = [];
      return;
    }
    try {
      const st = fs.statSync(this.rulesFile);
      const sig = `${st.mtimeMs}:${st.size}`;
      if (sig === this._sig) return;
      this._sig = sig;
      const text = fs.readFileSync(this.rulesFile, 'utf-8');
      this._rules = text.split(/\r?\n/).map(parseRepoRuleLine).filter(Boolean);
    } catch {
      this._sig = null;
      this._rules = [];
    }
  }

  getRules() {
    this._load();
    return this._rules.map((r) => r.raw);
  }

  isAllowed(owner, repo) {
    this._load();
    const target = `${owner}/${repo}`.toLowerCase();
    let allowed = false;
    for (const rule of this._rules) {
      const pat = rule.pattern.toLowerCase();
      if (fnmatchCase(target, pat) || globToRegExp(pat).test(target)) {
        allowed = !rule.negated;
      }
    }
    return allowed;
  }
}

function parseRepoAliases(raw) {
  const aliases = {};
  for (const part of String(raw || '').split(',')) {
    const trimmed = part.trim();
    if (!trimmed || !trimmed.includes('=')) continue;
    const eq = trimmed.indexOf('=');
    const alias = trimmed.slice(0, eq).trim().replace(/^\/+|\/+$/g, '');
    const target = trimmed.slice(eq + 1).trim().replace(/^\/+|\/+$/g, '');
    if (alias && target) aliases[alias] = target;
  }
  return aliases;
}

function parseGhHostsToken(hostsFile, targetHost = 'github.com') {
  try {
    if (!hostsFile || !fs.existsSync(hostsFile)) return '';
    const text = fs.readFileSync(hostsFile, 'utf-8');
    let inTargetHost = false;
    let hostIndent = -1;
    for (const rawLine of text.split(/\r?\n/)) {
      if (!rawLine.trim() || rawLine.trim().startsWith('#')) continue;
      const indent = rawLine.length - rawLine.trimStart().length;
      const trimmed = rawLine.trim();
      if (/^[A-Za-z0-9_.-]+:\s*$/.test(trimmed) && indent === 0) {
        inTargetHost = trimmed.slice(0, -1).toLowerCase() === targetHost.toLowerCase();
        hostIndent = indent;
        continue;
      }
      if (inTargetHost && indent <= hostIndent) {
        inTargetHost = false;
      }
      if (inTargetHost) {
        const m = /^oauth_token:\s*(.+)$/.exec(trimmed);
        if (m) {
          return m[1].replace(/^['"]|['"]$/g, '').trim();
        }
      }
    }
  } catch {
    // ignore missing or unreadable gh hosts.yml
  }
  return '';
}

class Settings {
  constructor(overrides = {}) {
    this.host = overrides.host ?? process.env.GATEWAY_HOST ?? '127.0.0.1';
    this.port = overrides.port ?? parseIntEnv('GATEWAY_PORT', 8000);
    this.publicBaseUrl = (overrides.publicBaseUrl ?? process.env.PUBLIC_BASE_URL ?? '').replace(/\/+$/, '');
    this.trustProxyHeaders = overrides.trustProxyHeaders ?? parseBoolEnv('TRUST_PROXY_HEADERS', true);

    this.explicitGithubToken = overrides.githubToken !== undefined ? overrides.githubToken : null;
    this.githubToken = overrides.githubToken ?? process.env.GITHUB_TOKEN ?? process.env.GH_TOKEN ?? '';
    this.useGhCli = overrides.useGhCli ?? parseBoolEnv('USE_GH_CLI', true);
    this.allowGhCliFallback = overrides.allowGhCliFallback ?? true;
    this.ghConfigDir = overrides.ghConfigDir ?? process.env.GH_CONFIG_DIR ?? path.join(os.homedir(), '.config', 'gh');
    this.ghCliPath = overrides.ghCliPath ?? process.env.GH_CLI_PATH ?? 'gh';
    this.githubWebBase = (overrides.githubWebBase ?? process.env.GITHUB_WEB_BASE ?? 'https://github.com').replace(/\/+$/, '');
    this.githubApiBase = (overrides.githubApiBase ?? process.env.GITHUB_API_BASE ?? 'https://api.github.com').replace(/\/+$/, '');

    this.adminToken = overrides.adminToken ?? process.env.ADMIN_TOKEN ?? '';
    this.defaultBaseBranch = overrides.defaultBaseBranch ?? process.env.DEFAULT_BASE_BRANCH ?? 'main';

    const rawContentDir = overrides.contentDir !== undefined ? overrides.contentDir : (process.env.CONTENT_DIR || '');
    this.contentDir = rawContentDir ? path.resolve(rawContentDir) : null;

    const rawTempDir = overrides.tempDir ?? process.env.TEMP_DIR ?? path.join(os.tmpdir(), 'git-gateway');
    this.tempDir = path.resolve(rawTempDir);

    this.repoRulesFile = overrides.repoRulesFile !== undefined
      ? (overrides.repoRulesFile ? path.resolve(overrides.repoRulesFile) : null)
      : (process.env.REPO_RULES_FILE ? path.resolve(process.env.REPO_RULES_FILE) : null);

    this.inlineRules = overrides.inlineRules !== undefined
      ? overrides.inlineRules
      : (process.env.ALLOWED_REPOS
        ? process.env.ALLOWED_REPOS.split(',').map((s) => s.trim()).filter(Boolean)
        : null);

    this.repoAliases = overrides.repoAliases ?? parseRepoAliases(process.env.REPO_ALIASES || '');

    this.blockedPatchPaths = overrides.blockedPatchPaths ?? [...DEFAULT_BLOCKED_PATHS];
    this.allowSymlinkPatches = overrides.allowSymlinkPatches ?? parseBoolEnv('ALLOW_SYMLINK_PATCHES', false);
    this.maxPatchBytes = overrides.maxPatchBytes ?? parseIntEnv('MAX_PATCH_BYTES', 5 * 1024 * 1024);
    this.maxGitProxyBodyBytes = overrides.maxGitProxyBodyBytes ?? parseIntEnv('MAX_GIT_PROXY_BODY_BYTES', 16 * 1024 * 1024);
    this.maxLogBytes = overrides.maxLogBytes ?? parseIntEnv('MAX_LOG_BYTES', 2 * 1024 * 1024);
    this.gitTimeoutSeconds = overrides.gitTimeoutSeconds ?? parseIntEnv('GIT_TIMEOUT_SECONDS', 120);

    this._matcher = new RepoRuleMatcher({
      rulesFile: this.repoRulesFile,
      inlineRules: this.inlineRules,
    });

    this._tokenResolved = false;
    this._cachedToken = '';
    this._tokenSource = 'none';
  }

  activeRules() {
    return this._matcher.getRules();
  }

  isRepoAllowed(owner, repo) {
    return this._matcher.isAllowed(owner, repo);
  }

  resolveGithubToken() {
    if (this.explicitGithubToken) {
      this._tokenSource = 'env/arg';
      return this.explicitGithubToken;
    }
    if (this._tokenResolved) return this._cachedToken;
    this._tokenResolved = true;

    // 1. Prefer borrowing host gh credentials (hosts.yml mount or GH_CLI_PATH / gh CLI)
    if (this.useGhCli && this.allowGhCliFallback) {
      const yamlToken = parseGhHostsToken(path.join(this.ghConfigDir, 'hosts.yml'));
      if (yamlToken) {
        this._cachedToken = yamlToken;
        this._tokenSource = 'gh_hosts_yml';
        return yamlToken;
      }
      try {
        const out = execFileSync(this.ghCliPath, ['auth', 'token'], {
          encoding: 'utf-8',
          timeout: 5000,
          stdio: ['ignore', 'pipe', 'ignore'],
        }).trim();
        if (out) {
          this._cachedToken = out;
          this._tokenSource = 'gh_cli';
          return out;
        }
      } catch {
        // gh not installed or not logged in
      }
    }

    // 2. Fallback to explicit API key env (GH_TOKEN / GITHUB_TOKEN)
    if (this.githubToken) {
      this._cachedToken = this.githubToken;
      this._tokenSource = 'env/arg';
      return this.githubToken;
    }

    this._tokenSource = 'none';
    return '';
  }

  tokenSource() {
    this.resolveGithubToken();
    return this._tokenSource;
  }

  tokenHint() {
    if (this.resolveGithubToken()) return null;
    return `No GitHub token found. Mount host gh config dir (${this.ghConfigDir}/hosts.yml via GH_CONFIG_DIR / GH_HOST_CONFIG_DIR), set GH_CLI_PATH, or configure GH_TOKEN / GITHUB_TOKEN.`;
  }

  isPatchPathBlocked(relPath) {
    let normalized = String(relPath || '').replace(/\\/g, '/').trim();
    if (!normalized) return false;
    normalized = posix.normalize(normalized).replace(/^\/+/, '');
    if (normalized === '.' || normalized === '..' || normalized.startsWith('../')) {
      return true;
    }
    for (const pat of this.blockedPatchPaths) {
      const patNorm = pat.trim().replace(/^\/+/, '');
      if (fnmatchCase(normalized, patNorm)) return true;
    }
    return false;
  }

  resolveRepoPath(rawPath) {
    const cleaned = String(rawPath || '').replace(/^\/+|\/+$/g, '');
    if (!cleaned) return null;
    const cleanedNoGit = cleaned.endsWith('.git') ? cleaned.slice(0, -4) : cleaned;

    if (Object.prototype.hasOwnProperty.call(this.repoAliases, cleanedNoGit)) {
      const target = this.repoAliases[cleanedNoGit].replace(/^\/+|\/+$/g, '');
      const parts = target.split('/');
      if (parts.length === 2 && parts[0] && parts[1]) {
        return { repoPath: cleanedNoGit, owner: parts[0], repo: parts[1] };
      }
    }

    const parts = cleaned.split('/');
    if (parts.length === 2 && parts[0] && parts[1]) {
      const owner = parts[0];
      const repo = parts[1].endsWith('.git') ? parts[1].slice(0, -4) : parts[1];
      if (owner && repo) {
        return { repoPath: `${owner}/${repo}`, owner, repo };
      }
    }
    return null;
  }

  splitRepoAndSuffix(fullPath) {
    const cleaned = String(fullPath || '').replace(/^\/+|\/+$/g, '');
    if (!cleaned) return null;

    // 1. Check longest alias prefix first
    const sortedAliases = Object.keys(this.repoAliases).sort((a, b) => b.length - a.length);
    for (const alias of sortedAliases) {
      for (const candidate of [`${alias}.git`, alias]) {
        if (cleaned === candidate || cleaned.startsWith(`${candidate}/`)) {
          const resolved = this.resolveRepoPath(alias);
          if (!resolved) continue;
          const suffix = cleaned.length > candidate.length ? cleaned.slice(candidate.length + 1) : '';
          return { ...resolved, suffix };
        }
      }
    }

    // 2. Fallback to <owner>/<repo>[.git][/<suffix>]
    const parts = cleaned.split('/');
    if (parts.length < 2) return null;
    const owner = parts[0];
    let repo = parts[1];
    if (repo.endsWith('.git')) repo = repo.slice(0, -4);
    if (!owner || !repo) return null;
    const suffix = parts.slice(2).join('/');
    return { repoPath: `${owner}/${repo}`, owner, repo, suffix };
  }
}

module.exports = {
  Settings,
  RepoRuleMatcher,
  DEFAULT_BLOCKED_PATHS,
  parseBoolEnv,
  parseRepoAliases,
};
