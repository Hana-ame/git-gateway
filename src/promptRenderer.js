// src/promptRenderer.js — Render agent onboarding guide and TODO pages
'use strict';

function buildTemplateVars(settings, baseUrl, repoPath, owner, repo) {
  const base = String(baseUrl || '').replace(/\/+$/, '');
  const repoBase = `${base}/${repoPath}`;
  return {
    PROXY_BASE: base,
    OWNER: owner,
    REPO: repo,
    REPO_PATH: repoPath,
    GITHUB_URL: `${settings.githubWebBase}/${owner}/${repo}.git`,
    GITHUB_WEB_URL: `${settings.githubWebBase}/${owner}/${repo}`,
    CLONE_PROXY_URL: `${repoBase}.git`,
    TODO_URL: `${repoBase}/todo`,
    PATCH_URL: `${repoBase}/patches`,
    PRS_URL: `${repoBase}/prs`,
    DEFAULT_BASE_BRANCH: settings.defaultBaseBranch,
  };
}

function applyTemplate(text, variables) {
  let rendered = String(text || '');
  for (const [k, v] of Object.entries(variables)) {
    rendered = rendered.split(`{{${k}}}`).join(v);
  }
  return rendered;
}

function renderAgentLanding(settings, store, baseUrl, repoPath, owner, repo) {
  const vars = buildTemplateVars(settings, baseUrl, repoPath, owner, repo);
  const promptEntry = store.getRepoPrompt(owner, repo);
  const todoEntry = store.getTodo(owner, repo);

  const renderedPrompt = applyTemplate(promptEntry.content, vars).trim();
  const renderedTodo = applyTemplate(todoEntry.content, vars).trim();
  const markdown = `${renderedPrompt}\n\n---\n\n## Current TODO Snapshot (\`GET ${vars.TODO_URL}\`)\n\n${renderedTodo}\n`;

  return {
    repo: `${owner}/${repo}`,
    repo_path: repoPath,
    github_url: vars.GITHUB_URL,
    clone_proxy_url: vars.CLONE_PROXY_URL,
    todo_url: vars.TODO_URL,
    patch_url: vars.PATCH_URL,
    prs_url: vars.PRS_URL,
    prompt_scope: promptEntry.scope || 'global',
    prompt_updated_at: promptEntry.updated_at,
    todo_updated_at: todoEntry.updated_at,
    prompt: renderedPrompt,
    todo: renderedTodo,
    markdown,
  };
}

function renderTodoPage(settings, store, baseUrl, repoPath, owner, repo) {
  const vars = buildTemplateVars(settings, baseUrl, repoPath, owner, repo);
  const todoEntry = store.getTodo(owner, repo);
  const content = `${applyTemplate(todoEntry.content, vars).trim()}\n`;
  return {
    repo: `${owner}/${repo}`,
    repo_path: repoPath,
    updated_at: todoEntry.updated_at,
    is_default: Boolean(todoEntry.is_default),
    content,
  };
}

module.exports = { buildTemplateVars, applyTemplate, renderAgentLanding, renderTodoPage };
