#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_DIR}"

if ! command -v python3 >/dev/null 2>&1; then
    echo "[ERROR] 未找到 python3，请先安装 Python 3.10+。" >&2
    exit 1
fi

if ! command -v git >/dev/null 2>&1; then
    echo "[ERROR] 未找到 git，请先安装 git。" >&2
    exit 1
fi

if [ ! -d ".venv" ]; then
    echo "[INFO] 创建 Python 虚拟环境 .venv ..."
    python3 -m venv .venv
fi

echo "[INFO] 安装/校验依赖 ..."
.venv/bin/pip install --upgrade pip >/dev/null
.venv/bin/pip install -r requirements.txt >/dev/null

if [ -f ".env" ]; then
    echo "[INFO] 加载 .env 环境变量 ..."
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
else
    echo "[WARN] 未找到 .env 文件，将使用当前 Shell 环境变量或默认值。"
    echo "       建议先执行: cp .env.example .env && vim .env"
fi

echo "[INFO] 启动 git-gateway (临时文件使用系统 /tmp，零本地持久化存储) ..."
exec .venv/bin/python main.py "$@"
