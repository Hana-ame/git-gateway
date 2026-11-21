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
GH_CONF_DIR="${CONF_DIR}/gh"
ENV_FILE="${CONF_DIR}/env"
RULES_FILE="${CONF_DIR}/repos.txt"
CONTENT_DIR="/var/lib/git-gateway/content"
SERVICE_USER="git-gateway"

echo "[1/6] 检查依赖 (python3, git, gh) ..."
for cmd in python3 git; do
    if ! command -v "${cmd}" >/dev/null 2>&1; then
        echo "[ERROR] 缺少 ${cmd}，请先安装 (例如: apt-get install -y python3 python3-venv git)" >&2
        exit 1
    fi
done
if ! command -v gh >/dev/null 2>&1; then
    echo "[WARN] 未检测到 gh CLI。默认推荐使用 gh 管理认证，请参考 https://cli.github.com/ 安装。"
fi

echo "[2/6] 创建系统用户 ${SERVICE_USER} ..."
if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

echo "[3/6] 同步程序代码到 ${INSTALL_DIR} ..."
mkdir -p "${INSTALL_DIR}" "${CONF_DIR}" "${GH_CONF_DIR}"
# Admin prompt/TODO 持久化目录（systemd 的 StateDirectory 也会创建，这里提前建好
# 以便非 systemd 场景/首次安装时目录属主正确）
mkdir -p "${CONTENT_DIR}"
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
    echo "GH_CONFIG_DIR=${GH_CONF_DIR}" >> "${ENV_FILE}"
    echo "CONTENT_DIR=${CONTENT_DIR}" >> "${ENV_FILE}"
    chmod 600 "${ENV_FILE}"
    chown "${SERVICE_USER}:${SERVICE_USER}" "${ENV_FILE}"
fi

chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}" "${GH_CONF_DIR}" "${CONTENT_DIR}"
chmod 700 "${GH_CONF_DIR}"
chmod 750 "${CONTENT_DIR}"

echo "[6/6] 安装并启用 systemd 服务 ..."
cp "${SRC_DIR}/deploy/git-gateway.service" /etc/systemd/system/git-gateway.service
systemctl daemon-reload
systemctl enable git-gateway

echo ""
echo "============================================================"
echo "安装完成（默认使用 gh CLI 管理认证；Admin 内容持久化到 ${CONTENT_DIR}）！"
echo "下一步操作："
echo "  1. 使用 gh 登录认证（保存到 ${GH_CONF_DIR}）："
echo "       sudo GH_CONFIG_DIR=${GH_CONF_DIR} gh auth login"
echo "       sudo chown -R ${SERVICE_USER}:${SERVICE_USER} ${GH_CONF_DIR}"
echo "  2. 编辑配置与白名单："
echo "       sudo vim ${ENV_FILE}"
echo "       sudo vim ${RULES_FILE}  (支持热重载)"
echo "  3. 启动并验收："
echo "       sudo systemctl restart git-gateway"
echo "       sudo systemctl status git-gateway"
echo "       curl -s http://127.0.0.1:8000/health"
echo "============================================================"
