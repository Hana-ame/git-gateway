// src/models.js — Strict validation for nicknames, git branches, commits, and patch submissions
'use strict';

const NICKNAME_RE = /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,31}$/;
const RESERVED_NICKNAMES = new Set([
  'main', 'master', 'head', 'refs', 'origin',
  'admin', 'system', 'release', 'tags', 'heads',
]);
const COMMIT_SHA_RE = /^[0-9a-fA-F]{7,40}$/;
const BRANCH_BAD_RE = /(^\/|\/$|\/\/|\.\.|@\{|[\x00-\x20\x7f~^:?*[\\])/;
const VALID_PATCH_MODES = new Set(['auto', 'incremental', 'cumulative']);

function validateNickname(value) {
  const nick = String(value || '').trim();
  if (!nick) {
    throw new Error('nickname is required (ask the user to set a nickname first)');
  }
  if (!NICKNAME_RE.test(nick)) {
    throw new Error('nickname must be 1-32 chars, start with alphanumeric, and contain only [a-zA-Z0-9_-]');
  }
  if (RESERVED_NICKNAMES.has(nick.toLowerCase())) {
    throw new Error(`nickname '${nick}' is reserved; choose a personal handle`);
  }
  return nick;
}

function validateBranchName(value) {
  let branch = String(value || '').trim();
  if (!branch) {
    throw new Error("base_branch is required (e.g. 'main' or 'feat/login')");
  }
  if (branch.startsWith('refs/heads/')) {
    branch = branch.slice('refs/heads/'.length);
  }
  if (
    BRANCH_BAD_RE.test(branch) ||
    branch.endsWith('.lock') ||
    branch.endsWith('.') ||
    branch === '@' ||
    branch === 'HEAD'
  ) {
    throw new Error(`invalid git branch name: '${value}'`);
  }
  if (branch.length > 128) {
    throw new Error('branch name too long (max 128 chars)');
  }
  return branch;
}

function buildAgentBranch(nickname, originalBranch) {
  const nick = validateNickname(nickname);
  let orig = validateBranchName(originalBranch);
  const prefix = `${nick}/`;
  if (orig.startsWith(prefix)) {
    orig = validateBranchName(orig.slice(prefix.length));
  }
  return `${nick}/${orig}`;
}

function validatePatchSubmission(input) {
  if (!input || typeof input !== 'object') {
    throw new Error('submission payload must be an object');
  }
  const nickname = validateNickname(input.nickname);
  const base_branch = validateBranchName(input.base_branch);

  let base_commit = null;
  if (input.base_commit !== undefined && input.base_commit !== null && String(input.base_commit).trim()) {
    const sha = String(input.base_commit).trim();
    if (!COMMIT_SHA_RE.test(sha)) {
      throw new Error('base_commit must be a 7-40 char hex commit SHA');
    }
    base_commit = sha;
  }

  const message = String(input.message || '').trim();
  if (!message || message.length > 2000) {
    throw new Error('message must be 1-2000 chars');
  }

  const patch = String(input.patch || '');
  if (!patch.trim()) {
    throw new Error('patch cannot be empty');
  }

  const pr_title = input.pr_title != null ? String(input.pr_title).slice(0, 256) : null;
  const pr_body = input.pr_body != null ? String(input.pr_body).slice(0, 16000) : null;
  const create_pr = input.create_pr !== undefined ? Boolean(input.create_pr) : true;
  const patch_mode = input.patch_mode ? String(input.patch_mode) : 'auto';
  if (!VALID_PATCH_MODES.has(patch_mode)) {
    throw new Error(`invalid patch_mode: '${patch_mode}'`);
  }

  return {
    nickname,
    base_branch,
    base_commit,
    message,
    patch,
    pr_title,
    pr_body,
    create_pr,
    patch_mode,
  };
}

module.exports = {
  COMMIT_SHA_RE,
  validateNickname,
  validateBranchName,
  buildAgentBranch,
  validatePatchSubmission,
};
