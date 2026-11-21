"""CLI entrypoint for Agent Git Gateway."""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from gateway.app import create_app
from gateway.config import load_settings_from_env, parse_aliases


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Zero-permission AI Agent crowdsourcing Git proxy & patch-to-PR gateway"
    )
    parser.add_argument("--host", default=None, help="Bind host (default: 0.0.0.0 or GATEWAY_HOST)")
    parser.add_argument("--port", type=int, default=None, help="Bind port (default: 8000 or GATEWAY_PORT)")
    parser.add_argument("--public-url", default=None, help="Public base URL, e.g. https://proxy.com")
    parser.add_argument("--data-dir", default=None, help="Persistent data directory (mirrors + prompts + todos)")
    parser.add_argument("--base", default=None, help="Default base branch (default: main)")
    parser.add_argument(
        "--allow",
        action="append",
        default=[],
        help="Allowed GitHub repo pattern (owner/repo or owner/*), repeatable",
    )
    parser.add_argument(
        "--alias",
        action="append",
        default=[],
        help="Custom path alias mapping, e.g. path/to/repo=Hana-ame/repo (repeatable)",
    )
    parser.add_argument("--admin-token", default=None, help="Admin Bearer token for /admin/* endpoints")
    parser.add_argument("--github-token", default=None, help="GitHub PAT (defaults to GITHUB_TOKEN env or gh CLI)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = load_settings_from_env()

    if args.host:
        settings.host = args.host
    if args.port:
        settings.port = args.port
    if args.public_url:
        settings.public_base_url = args.public_url.rstrip("/")
    if args.data_dir:
        settings.data_dir = Path(args.data_dir)
    if args.base:
        settings.default_base_branch = args.base
    if args.allow:
        settings.allowed_repos = args.allow
    if args.alias:
        for entry in args.alias:
            settings.repo_aliases.update(parse_aliases(entry))
    if args.admin_token:
        settings.admin_token = args.admin_token
    if args.github_token:
        settings.github_token = args.github_token

    app = create_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
