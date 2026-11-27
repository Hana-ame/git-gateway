// src/gitOps.js — Ephemeral Git patch-application engine with double-layer CI poisoning protection
'use strict';

const fs = require('fs');
const fsp = fs.promises;
const path = require('path');
const { execFile } = require('child_process');
const { COMMIT_SHA_RE, buildAgentBranch } = require('./models');

const DIFF_GIT_RE = /^diff --git (?:"?a\/(.+?)"?) (?:"?b\/(.+?)"?)$/;
const PLUS_FILE_RE = /^\+\+\+ (?:"?(?:b\/)?(.+?)"?)$/;
const MINUS_FILE_RE = /^--- (?:"?(?:a\/)?(.+?)"?)$/;
const RENAME_RE = /^(?:rename|copy) (?:from|to) (?:"?(.+?)"?)$/;
const BASE_COMMIT_TRAILER_RE = /^base-commit:\s*([0-9a-fA-F]{7,40})\s*$/m;

class PatchValidationError extends Error {
  constructor(statusCode, error, detail = '') {
    super(detail || error);
    this.statusCode = statusCode;
    this.error = error;
    this.detail = detail;
  }
}

function decodeGitCQuoted(raw) {
  let s = String(raw || '').trim();
  if (s.length >= 2 && s.startsWith('"') && s.endsWith('"')) {
    s = s.slice(1, -1);
  }
  const simple = {
    a: 0x07, b: 0x08, f: 0x0c, n: 0x0a, r: 0x0d, t: 0x09, v: 0x0b, '\\': 0x5c, '"': 0x22,
  };
  const bytes = [];
  for (let i = 0; i < s.length; ) {
    const ch = s[i];
    if (ch === '\\' && i + 1 < s.length) {
      const nxt = s[i + 1];
      if (Object.prototype.hasOwnProperty.call(simple, nxt)) {
        bytes.push(simple[nxt]);
        i += 2;
        continue;
      }
      if (nxt >= '0' && nxt <= '7') {
        let oct = nxt;
        let j = i + 2;
        while (j < s.length && oct.length < 3 && s[j] >= '0' && s[j] <= '7') {
          oct += s[j];
          j++;
        }
        bytes.push(Number.parseInt(oct, 8) & 0xff);
        i = j;
        continue;
      }
    }
    const encoded = Buffer.from(ch, 'utf-8');
    for (const b of encoded) bytes.push(b);
    i++;
  }
  return Buffer.from(bytes).toString('utf-8');
}

function stripAbPrefix(raw) {
  const decoded = decodeGitCQuoted(raw);
  if (decoded.startsWith('a/') || decoded.startsWith('b/')) return decoded.slice(2);
  return decoded;
}

function extractTouchedPaths(patchText) {
  const paths = new Set();
  for (const line of String(patchText || '').split(/\r?\n/)) {
    let m = DIFF_GIT_RE.exec(line);
    if (m) {
      paths.add(stripAbPrefix(m[1]));
      paths.add(stripAbPrefix(m[2]));
      continue;
    }
    m = PLUS_FILE_RE.exec(line);
    if (m) {
      const raw = m[1].split('\t')[0];
      if (raw !== '/dev/null') paths.add(stripAbPrefix(raw));
      continue;
    }
    m = MINUS_FILE_RE.exec(line);
    if (m) {
      const raw = m[1].split('\t')[0];
      if (raw !== '/dev/null') paths.add(stripAbPrefix(raw));
      continue;
    }
    m = RENAME_RE.exec(line);
    if (m) paths.add(decodeGitCQuoted(m[1]));
  }
  return Array.from(paths).sort();
}

function detectBaseCommit(patchText) {
  const m = BASE_COMMIT_TRAILER_RE.exec(String(patchText || ''));
  return m && COMMIT_SHA_RE.test(m[1]) ? m[1] : null;
}

function sanitizeGitError(raw) {
  const lines = String(raw || '')
    .split(/\r?\n/)
    .map((line) =>
      line
        .replace(/Authorization:\s*\S+\s*\S*/gi, 'Authorization: [REDACTED]')
        .replace(/(\/tmp|\/var\/tmp|[A-Za-z]:\\[^\s'" ]*Temp)[^\s'" ]*/gi, '<temp>')
    );
  const text = lines.join('\n').trim();
  return text ? text.slice(0, 600) : 'git operation failed';
}

class GitPatchEngine {
  constructor(settings) {
    this.settings = settings;
    this.workDir = path.join(settings.tempDir, 'work');
    fs.mkdirSync(this.workDir, { recursive: true });
    this._repoChains = new Map();
  }

  _withRepoLock(owner, repo, fn) {
    const key = `${owner}/${repo}`;
    const prev = this._repoChains.get(key) || Promise.resolve();
    const next = prev.catch(() => {}).then(fn);
    this._repoChains.set(key, next);
    next.finally(() => {
      if (this._repoChains.get(key) === next) this._repoChains.delete(key);
    }).catch(() => {});
    return next;
  }

  _remoteUrl(owner, repo) {
    return `${this.settings.githubWebBase}/${owner}/${repo}.git`;
  }

  _gitEnv({ withAuth = false, extraEnv = null } = {}) {
    const env = { ...process.env, GIT_TERMINAL_PROMPT: '0', GIT_ASKPASS: 'echo' };
    if (withAuth) {
      const tok = this.settings.resolveGithubToken();
      if (tok) {
        const encoded = Buffer.from(`x-access-token:${tok}`, 'utf-8').toString('base64');
        const count = Number.parseInt(env.GIT_CONFIG_COUNT || '0', 10) || 0;
        env[`GIT_CONFIG_KEY_${count}`] = 'http.extraHeader';
        env[`GIT_CONFIG_VALUE_${count}`] = `Authorization: Basic ${encoded}`;
        env.GIT_CONFIG_COUNT = String(count + 1);
      }
    }
    if (extraEnv) Object.assign(env, extraEnv);
    return env;
  }

  _runGit(args, { cwd = undefined, withAuth = false, extraEnv = null, binary = false } = {}) {
    return new Promise((resolve) => {
      execFile(
        'git',
        args,
        {
          cwd,
          env: this._gitEnv({ withAuth, extraEnv }),
          timeout: this.settings.gitTimeoutSeconds * 1000,
          maxBuffer: 20 * 1024 * 1024,
          encoding: binary ? 'buffer' : 'utf-8',
        },
        (err, stdout, stderr) => {
          const code = err ? (typeof err.code === 'number' ? err.code : -1) : 0;
          if (binary) {
            resolve({
              code,
              stdout: Buffer.isBuffer(stdout) ? stdout : Buffer.from(stdout || ''),
              stderr: Buffer.isBuffer(stderr) ? stderr : Buffer.from(stderr || ''),
            });
          } else {
            resolve({ code, output: String(stdout || '') + String(stderr || '') });
          }
        }
      );
    });
  }

  async _refSha(repoDir, ref) {
    const { code, output } = await this._runGit(['rev-parse', '--verify', '--quiet', `${ref}^{commit}`], {
      cwd: repoDir,
    });
    return code === 0 && output.trim() ? output.trim() : null;
  }

  async _tryApply(workdir, patchFile, useThreeWay = false) {
    const args = ['apply', '--whitespace=nowarn'];
    if (useThreeWay) args.push('--3way');
    const check = await this._runGit([...args, '--check', patchFile], { cwd: workdir });
    if (check.code !== 0 && !useThreeWay) return { ok: false, err: check.output.trim() };
    const applied = await this._runGit([...args, patchFile], { cwd: workdir });
    return { ok: applied.code === 0, err: applied.output.trim() };
  }

  async _verifyTreeSecurity(workdir, baseTreeRef, newTree) {
    const { code, stdout } = await this._runGit(
      ['diff-tree', '-r', '--raw', '-z', '--no-renames', baseTreeRef, newTree],
      { cwd: workdir, binary: true }
    );
    if (code !== 0) {
      throw new PatchValidationError(500, 'security verification failed', 'unable to inspect resulting tree');
    }

    // Split Buffer by NUL byte 0x00
    const tokens = [];
    let start = 0;
    for (let i = 0; i < stdout.length; i++) {
      if (stdout[i] === 0) {
        tokens.push(stdout.subarray(start, i));
        start = i + 1;
      }
    }
    if (start < stdout.length) tokens.push(stdout.subarray(start));

    const blockedHits = new Set();
    const symlinkHits = new Set();
    for (let i = 0; i + 1 < tokens.length; i += 2) {
      const meta = tokens[i];
      const pathBuf = tokens[i + 1];
      if (!meta || meta.length === 0) continue;
      const pathStr = pathBuf.toString('utf-8');
      if (this.settings.isPatchPathBlocked(pathStr)) blockedHits.add(pathStr);

      const parts = meta.toString('ascii').replace(/^:/, '').split(/\s+/);
      if (parts.length >= 2 && !this.settings.allowSymlinkPatches) {
        if (parts[0] === '120000' || parts[1] === '120000') symlinkHits.add(pathStr);
      }
    }

    if (blockedHits.size > 0) {
      throw new PatchValidationError(
        403,
        'patch modifies protected paths',
        `blocked paths: ${Array.from(blockedHits).sort().join(', ')}`
      );
    }
    if (symlinkHits.size > 0) {
      throw new PatchValidationError(
        403,
        'symlink modifications are blocked for security',
        `symlink paths: ${Array.from(symlinkHits).sort().join(', ')}`
      );
    }
  }

  async applyAndPush(owner, repo, { nickname, baseBranch, baseCommit, message, patchText, patchMode = 'auto' }) {
    const t0 = Date.now();

    // 1. Fast-fail pre-apply security check
    const touched = extractTouchedPaths(patchText);
    const preBlocked = touched.filter((p) => this.settings.isPatchPathBlocked(p));
    if (preBlocked.length > 0) {
      throw new PatchValidationError(403, 'patch modifies protected paths', `blocked paths: ${preBlocked.join(', ')}`);
    }

    const targetBranch = buildAgentBranch(nickname, baseBranch);
    const detectedBase = baseCommit || detectBaseCommit(patchText);

    return this._withRepoLock(owner, repo, async () => {
      await fsp.mkdir(this.workDir, { recursive: true });
      const tmpRoot = await fsp.mkdtemp(path.join(this.workDir, 'ggw-req-'));
      const bareDir = path.join(tmpRoot, 'bare.git');
      const workdir = path.join(tmpRoot, 'wt');
      const patchFile = path.join(tmpRoot, 'submit.patch');

      try {
        const cloneRes = await this._runGit(['clone', '--bare', this._remoteUrl(owner, repo), bareDir], {
          withAuth: true,
        });
        if (cloneRes.code !== 0) {
          throw new PatchValidationError(502, 'upstream clone failed', sanitizeGitError(cloneRes.output));
        }

        const baseBranchSha =
          (await this._refSha(bareDir, `refs/heads/${baseBranch}`)) ||
          (await this._refSha(bareDir, `refs/remotes/origin/${baseBranch}`));
        if (!baseBranchSha) {
          throw new PatchValidationError(400, 'base_branch not found', `branch '${baseBranch}' does not exist on ${owner}/${repo}`);
        }

        const existingTargetSha =
          (await this._refSha(bareDir, `refs/heads/${targetBranch}`)) ||
          (await this._refSha(bareDir, `refs/remotes/origin/${targetBranch}`));

        let resolvedBaseSha = baseBranchSha;
        if (detectedBase) {
          const cand = await this._refSha(bareDir, detectedBase);
          if (cand) resolvedBaseSha = cand;
        }

        let normalized = String(patchText || '').replace(/\r\n/g, '\n');
        if (normalized && !normalized.endsWith('\n')) normalized += '\n';
        await fsp.writeFile(patchFile, normalized, 'utf-8');

        const startSha = patchMode === 'cumulative' ? resolvedBaseSha : (existingTargetSha || resolvedBaseSha);
        const wtRes = await this._runGit(['worktree', 'add', '--detach', workdir, startSha], { cwd: bareDir });
        if (wtRes.code !== 0) {
          throw new PatchValidationError(500, 'failed to create worktree', sanitizeGitError(wtRes.output));
        }

        let appliedOnExisting = false;
        if (existingTargetSha && patchMode !== 'cumulative') {
          let r = await this._tryApply(workdir, patchFile, false);
          if (r.ok) {
            appliedOnExisting = true;
          } else {
            await this._runGit(['reset', '--hard', existingTargetSha], { cwd: workdir });
            await this._runGit(['clean', '-fdq'], { cwd: workdir });
            r = await this._tryApply(workdir, patchFile, true);
            if (r.ok) {
              appliedOnExisting = true;
            } else if (patchMode === 'incremental') {
              throw new PatchValidationError(400, 'incremental patch does not apply cleanly on existing branch', sanitizeGitError(r.err));
            } else {
              await this._runGit(['reset', '--hard', resolvedBaseSha], { cwd: workdir });
              await this._runGit(['clean', '-fdq'], { cwd: workdir });
              r = await this._tryApply(workdir, patchFile, false);
              if (!r.ok) {
                await this._runGit(['reset', '--hard', resolvedBaseSha], { cwd: workdir });
                await this._runGit(['clean', '-fdq'], { cwd: workdir });
                r = await this._tryApply(workdir, patchFile, true);
              }
              if (!r.ok) {
                throw new PatchValidationError(400, 'patch does not apply cleanly', sanitizeGitError(r.err));
              }
            }
          }
        } else {
          let r = await this._tryApply(workdir, patchFile, false);
          if (!r.ok) {
            await this._runGit(['reset', '--hard', resolvedBaseSha], { cwd: workdir });
            await this._runGit(['clean', '-fdq'], { cwd: workdir });
            r = await this._tryApply(workdir, patchFile, true);
          }
          if (!r.ok) {
            throw new PatchValidationError(400, 'patch does not apply cleanly', sanitizeGitError(r.err));
          }
        }

        await this._runGit(['add', '-A'], { cwd: workdir });
        const treeRes = await this._runGit(['write-tree'], { cwd: workdir });
        if (treeRes.code !== 0) {
          throw new PatchValidationError(500, 'git write-tree failed', sanitizeGitError(treeRes.output));
        }
        const newTree = treeRes.output.trim();
        const parentSha = existingTargetSha || resolvedBaseSha;

        // 2. Authoritative post-apply security verification against BOTH parent and base_branch
        await this._verifyTreeSecurity(workdir, parentSha, newTree);
        if (parentSha !== baseBranchSha) {
          await this._verifyTreeSecurity(workdir, baseBranchSha, newTree);
        }

        const parentTreeRes = await this._runGit(['rev-parse', `${parentSha}^{tree}`], { cwd: workdir });
        if (parentTreeRes.code === 0 && parentTreeRes.output.trim() === newTree) {
          return {
            status: 'noop',
            target_branch: targetBranch,
            base_branch: baseBranch,
            base_commit: resolvedBaseSha,
            commit_sha: parentSha,
            commit_short: parentSha.slice(0, 7),
            elapsed_ms: Date.now() - t0,
          };
        }

        let commitMsg = `${message.trim()}\n\nSubmitted-by: ${nickname}\nBase-branch: ${baseBranch}\nBase-commit: ${resolvedBaseSha}\n`;
        if (existingTargetSha && !appliedOnExisting) {
          commitMsg += 'Patch-mode: cumulative-snapshot\n';
        }
        const authorName = `${nickname} (agent)`;
        const authorEmail = `${nickname}@agents.local`;

        const commitRes = await this._runGit(['commit-tree', newTree, '-p', parentSha, '-m', commitMsg], {
          cwd: workdir,
          extraEnv: {
            GIT_AUTHOR_NAME: authorName,
            GIT_AUTHOR_EMAIL: authorEmail,
            GIT_COMMITTER_NAME: authorName,
            GIT_COMMITTER_EMAIL: authorEmail,
          },
        });
        if (commitRes.code !== 0) {
          throw new PatchValidationError(500, 'git commit-tree failed', sanitizeGitError(commitRes.output));
        }
        const newSha = commitRes.output.trim();

        const pushRes = await this._runGit(['push', 'origin', `${newSha}:refs/heads/${targetBranch}`], {
          cwd: bareDir,
          withAuth: true,
        });
        if (pushRes.code !== 0) {
          throw new PatchValidationError(502, 'push to upstream failed', sanitizeGitError(pushRes.output));
        }

        return {
          status: 'pushed',
          target_branch: targetBranch,
          base_branch: baseBranch,
          base_commit: resolvedBaseSha,
          commit_sha: newSha,
          commit_short: newSha.slice(0, 7),
          elapsed_ms: Date.now() - t0,
        };
      } finally {
        await fsp.rm(tmpRoot, { recursive: true, force: true }).catch(() => {});
      }
    });
  }
}

module.exports = {
  GitPatchEngine,
  PatchValidationError,
  decodeGitCQuoted,
  extractTouchedPaths,
  detectBaseCommit,
};
