"""Render admin-managed general instructions and TODO lists for AI Agents."""

from __future__ import annotations

from typing import Dict

from .config import Settings
from .store import ContentStore


def build_template_vars(
    settings: Settings,
    base_url: str,
    repo_path: str,
    owner: str,
    repo: str,
) -> Dict[str, str]:
    base = base_url.rstrip("/")
    repo_base = f"{base}/{repo_path}"
    return {
        "PROXY_BASE": base,
        "OWNER": owner,
        "REPO": repo,
        "REPO_PATH": repo_path,
        "GITHUB_URL": f"{settings.github_web_base}/{owner}/{repo}.git",
        "GITHUB_WEB_URL": f"{settings.github_web_base}/{owner}/{repo}",
        "CLONE_PROXY_URL": f"{repo_base}.git",
        "TODO_URL": f"{repo_base}/todo",
        "PATCH_URL": f"{repo_base}/patches",
        "PRS_URL": f"{repo_base}/prs",
        "DEFAULT_BASE_BRANCH": settings.default_base_branch,
    }


def apply_template(text: str, variables: Dict[str, str]) -> str:
    rendered = text
    for key, val in variables.items():
        rendered = rendered.replace("{{" + key + "}}", val)
    return rendered


def render_agent_landing(
    settings: Settings,
    store: ContentStore,
    base_url: str,
    repo_path: str,
    owner: str,
    repo: str,
) -> dict:
    """Build the rendered agent instructions + live TODO snapshot for `GET /<repo_path>`."""
    variables = build_template_vars(settings, base_url, repo_path, owner, repo)
    prompt_entry = store.get_repo_prompt(owner, repo)
    todo_entry = store.get_todo(owner, repo)

    rendered_prompt = apply_template(prompt_entry["content"], variables).strip()
    rendered_todo = apply_template(todo_entry["content"], variables).strip()

    combined_markdown = (
        f"{rendered_prompt}\n\n"
        f"---\n\n"
        f"## Current TODO Snapshot (`GET {variables['TODO_URL']}`)\n\n"
        f"{rendered_todo}\n"
    )

    return {
        "repo": f"{owner}/{repo}",
        "repo_path": repo_path,
        "github_url": variables["GITHUB_URL"],
        "clone_proxy_url": variables["CLONE_PROXY_URL"],
        "todo_url": variables["TODO_URL"],
        "patch_url": variables["PATCH_URL"],
        "prs_url": variables["PRS_URL"],
        "prompt_scope": prompt_entry.get("scope", "global"),
        "prompt_updated_at": prompt_entry.get("updated_at"),
        "todo_updated_at": todo_entry.get("updated_at"),
        "prompt": rendered_prompt,
        "todo": rendered_todo,
        "markdown": combined_markdown,
    }


def render_todo_page(
    settings: Settings,
    store: ContentStore,
    base_url: str,
    repo_path: str,
    owner: str,
    repo: str,
) -> dict:
    variables = build_template_vars(settings, base_url, repo_path, owner, repo)
    todo_entry = store.get_todo(owner, repo)
    rendered_todo = apply_template(todo_entry["content"], variables).strip() + "\n"
    return {
        "repo": f"{owner}/{repo}",
        "repo_path": repo_path,
        "updated_at": todo_entry.get("updated_at"),
        "is_default": todo_entry.get("is_default", False),
        "content": rendered_todo,
    }
