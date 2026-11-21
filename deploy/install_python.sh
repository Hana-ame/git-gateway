#!/usr/bin/env bash
set -euo pipefail

# 用法: sudo bash deploy/install_python.sh

if [ "${EUID}" -ne 0 ]; then
    echo "[ERROR] 请使用 root 或 sudo 运行此脚本。" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

INSTALL_DIR="/opt/git-gateway"
CONF_DIR="/etc/git-gateway"
ENV_FILE="${CONF_DIR}/env"
RULES_FILE="${CONF_DIR}/repos.txt"
SERVICE_USER="git-gateway"

echo "[1/6] 检查依赖 (python3, python3-venv, git) ..."
for cmd in python3 git; do
    if ! command -v "${cmd}" >/dev/null 2>&1; then
        echo "[ERROR] 缺少 ${cmd}，请先安装 (例如: apt-get install -y python3 python3-venv git)" >&2
        exit 1
    fi
done

echo "[2/6] 创建系统用户 ${SERVICE_USER} ..."
if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

echo "[3/6] 同步程序代码到 ${INSTALL_DIR} ..."
mkdir -p "${INSTALL_DIR}" "${CONF_DIR}"
cp -r "${SRC_DIR}/gateway" "${INSTALL_DIR}/"
cp "${SRC_DIR}/main.py" "${SRC_DIR}/requirements.txt" "${INSTALL_DIR}/"

echo "[4/6] 创建虚拟环境并安装 Python 依赖 ..."
if [ ! -d "${INSTALL_DIR}/.venv" ]; then
    python3 -m venv "${INSTALL_DIR}/.venv"
fi
"${INSTALL_DIR}/.venv/bin/pip" install --upgrade pip
"${INSTALL_DIR}/.venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"

echo "[5/6] 初始化配置 ${ENV_FILE} 与白名单/黑名单 ${RULES_FILE} ..."
if [ ! -f "${RULES_FILE}" ]; then
    cp "${SRC_DIR}/repos.txt.example" "${RULES_FILE}"
    chown root:"${SERVICE_USER}" "${RULES_FILE}"
    chmod 640 "${RULES_FILE}"
fi

if [ ! -f "${ENV_FILE}" ]; then
    cp "${SRC_DIR}/.env.example" "${ENV_FILE}"
    sed -i "s|^REPO_RULES_FILE=.*|REPO_RULES_FILE=${RULES_FILE}|" "${ENV_FILE}"
    chmod 600 "${ENV_FILE}"
    chown "${SERVICE_USER}:${SERVICE_USER}" "${ENV_FILE}"
    echo "[WARN] 已生成模板配置 ${ENV_FILE}，请务必编辑填入 GITHUB_TOKEN 与 ADMIN_TOKEN！"
else
    echo "[INFO] 检测到已有 ${ENV_FILE}，保留现有配置。"
fi

chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"

echo "[6/6] 安装并启用 systemd 服务 ..."
cp "${SRC_DIR}/deploy/git-gateway.service" /etc/systemd/system/git-gateway.service
systemctl daemon-reload
systemctl enable git-gateway

echo ""
echo "============================================================"
echo "安装完成（零本地持久化存储，临时文件全部落在私有 /tmp）！"
echo "下一步操作："
echo "  1. 编辑配置:   sudo vim ${ENV_FILE}"
echo "  2. 编辑白名单: sudo vim ${RULES_FILE}  (支持热重载)"
echo "  3. 启动服务:   sudo systemctl restart git-gateway"
echo "  4. 查看状态:   sudo systemctl status git-gateway"
echo "  5. 查看日志:   sudo journalctl -u git-gateway -f"
echo "  6. 健康检查:   curl -s http://127.0.0.1:8000/health"
echo "============================================================"
