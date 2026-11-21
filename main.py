"""CLI entrypoint for Agent Git Gateway."""

from __future__ import annotations

import argparse
import os
import sys
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
    parser.add_argument("--temp-dir", default=None, help="OS temp directory override (defaults to /tmp or %TEMP%)")
    parser.add_argument("--base", default=None, help="Default base branch (default: main)")
    parser.add_argument(
        "--rules-file",
        default=None,
        help="Path to gitignore-style repo allow/disallow rules file (default: repos.txt)",
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


def _print_startup_diagnostics(settings) -> None:
    proxy_vars = {
        k: os.environ[k]
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy", "NO_PROXY")
        if os.environ.get(k)
    }
    print("=" * 64, flush=True)
    print(f"git-gateway starting on http://{settings.host}:{settings.port}", flush=True)
    print(f"  PUBLIC_BASE_URL : {settings.public_base_url or '(auto from request headers)'}", flush=True)
    print(f"  TEMP_DIR        : {settings.temp_dir} (zero persistent local storage)", flush=True)
    print(f"  REPO_RULES_FILE : {settings.repo_rules_file or '(inline rules)'} ({len(settings.active_rules())} rules)", flush=True)
    print(f"  REPO_ALIASES    : {settings.repo_aliases or '(none - using /<owner>/<repo>)'}", flush=True)
    tok = settings.resolve_github_token()
    print(f"  GITHUB_AUTH     : {'configured (' + settings.token_source() + ')' if tok else 'NOT SET (run `gh auth login` or set GITHUB_TOKEN)'}", flush=True)
    print(f"  ADMIN_TOKEN     : {'configured' if settings.admin_token else 'NOT SET (admin disabled)'}", flush=True)
    if proxy_vars:
        print(f"  PROXY ENV       : {proxy_vars}", flush=True)
    if settings.host in ("127.0.0.1", "localhost"):
        print("  [NOTE] Bound to loopback only (127.0.0.1). External traffic must go through Nginx/Caddy.", flush=True)
    print("=" * 64, flush=True)


def main() -> None:
    args = parse_args()
    settings = load_settings_from_env()

    if args.host:
        settings.host = args.host
    if args.port:
        settings.port = args.port
    if args.public_url:
        settings.public_base_url = args.public_url.rstrip("/")
    if args.temp_dir:
        settings.temp_dir = Path(args.temp_dir)
    if args.base:
        settings.default_base_branch = args.base
    if args.rules_file:
        settings.set_rules_file(Path(args.rules_file))
    if args.alias:
        for entry in args.alias:
            settings.repo_aliases.update(parse_aliases(entry))
    if args.admin_token:
        settings.admin_token = args.admin_token
    if args.github_token:
        settings.github_token = args.github_token

    app = create_app(settings)
    _print_startup_diagnostics(settings)

    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        proxy_headers=settings.trust_proxy_headers,
        forwarded_allow_ips="*" if settings.trust_proxy_headers else "127.0.0.1",
        timeout_keep_alive=30,
        backlog=2048,
    )


if __name__ == "__main__":
    try:
        main()
    except OSError as exc:
        print(f"[FATAL] Failed to bind/start server: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
