#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_DIR}"

for cmd in python3 git; do
    if ! command -v "${cmd}" >/dev/null 2>&1; then
        echo "[ERROR] 未找到 ${cmd}，请先安装。" >&2
        exit 1
    fi
done

if [ -f ".env" ]; then
    echo "[INFO] 加载 .env 环境变量 ..."
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

if [ -z "${GITHUB_TOKEN:-}" ]; then
    if command -v gh >/dev/null 2>&1; then
        if gh auth status >/dev/null 2>&1; then
            echo "[INFO] 检测到已登录的 gh CLI，将默认通过 gh auth token 管理认证。"
        else
            echo "[WARN] 检测到 gh CLI 但尚未登录，请先运行: gh auth login"
        fi
    else
        echo "[WARN] 未安装 gh CLI 且未设置 GITHUB_TOKEN，请先安装 gh 并运行 gh auth login。"
    fi
fi

if [ ! -d ".venv" ]; then
    echo "[INFO] 创建 Python 虚拟环境 .venv ..."
    python3 -m venv .venv
fi

echo "[INFO] 安装/校验依赖 ..."
.venv/bin/pip install --upgrade pip >/dev/null
.venv/bin/pip install -r requirements.txt >/dev/null

echo "[INFO] 启动 git-gateway (Git 临时文件走系统 /tmp 并即时清理；Admin 内容持久化到 ${CONTENT_DIR:-内存模式}) ..."
exec .venv/bin/python main.py "$@"
