# git-gateway 部署与使用指南（Docker & Python 3 直接部署）

本版本已针对**网络稳定性**、**零本地持久化存储**和**gitignore 风格白名单/黑名单**进行了专门重构：
- **零本地存储**：不创建 `./data` 或 `/var/lib/git-gateway`，Prompt/TODO 驻留内存，所有临时 Git 克隆/Worktree/Patch 均自动写入系统临时目录（Linux/macOS 为 `/tmp/git-gateway`，Windows 为 `%TEMP%\git-gateway`），并在每次请求结束后立即清理。
- **gitignore 风格黑白名单**：通过纯文本 `repos.txt` 管理，支持热重载（改完文件无需重启）。
- **Python 网络加固**：消除事件循环阻塞、修复 Git Clone 代理压缩头问题、支持系统 SOCKS/HTTP 代理、显式配置 Uvicorn 反代头与监听诊断。

---

## 一、路径别名（`REPO_ALIASES`）与分支（`branch`）说明

### 1. 不配置 `REPO_ALIASES` 时，`path` 和 `repo` 如何对应？
当 `REPO_ALIASES` 留空（不映射）时，URL 的**前两段路径直接对应 GitHub 的 `<owner>/<repo>`**：

假设目标仓库是 `https://github.com/Hana-ame/my-project`：
- Agent 引导页：`GET https://proxy.com/Hana-ame/my-project`
- TODO 列表：`GET https://proxy.com/Hana-ame/my-project/todo`
- 只读 Clone：`git clone https://proxy.com/Hana-ame/my-project.git`
- 提交 Patch：`POST https://proxy.com/Hana-ame/my-project/patches`
- 查看 PR 列表：`GET https://proxy.com/Hana-ame/my-project/prs`
- 查看 CI / 日志：`GET https://proxy.com/Hana-ame/my-project/prs/1/ci`

如果配置了 `REPO_ALIASES=path/to/repo=Hana-ame/my-project`，则额外开放自定义前缀 `https://proxy.com/path/to/repo/...`（适合隐藏真实用户名/仓库名或使用多级路径），原 `/Hana-ame/my-project/...` 路径依然可用。

### 2. `branch`（分支）如何区分？
**分支名不会放在 URL 路径中**，因此完全不会与 `owner/repo` 或 `path/to/repo` 产生歧义：
- **提交 Patch 时**：通过 Query 参数（`?nickname=alice&base_branch=feat/ui`）或 JSON 字段（`"base_branch": "feat/ui"`）显式指定基准分支（支持带 `/` 的嵌套分支名；省略时默认取 `DEFAULT_BASE_BRANCH=main`）。
- **推送目标分支**：网关自动隔离推送到 `<nickname>/<base_branch>`（如 `alice/main`、`bob/feat/ui`），并自动创建或复用 `head=<nickname>/<base_branch>` → `base=<base_branch>` 的 PR。
- **查询 PR 时**：通过 Query 参数筛选，例如 `GET /Hana-ame/my-project/prs?nickname=alice&base_branch=feat/ui`。
- **Clone 时**：`git clone https://proxy.com/Hana-ame/my-project.git` 获取完整仓库，所有分支均在本地，正常 `git checkout <branch>` 即可。

---

## 二、白名单 / 黑名单规则（`repos.txt`）

通过 `REPO_RULES_FILE` 指定规则文件路径（默认 `./repos.txt`），语法与 `.gitignore` 一致，**从上到下依次匹配，最后一条匹配成功的规则生效**：

| 情况 | 结果 |
|---|---|
| 没有任何规则匹配到 | **disallow**（默认拒绝） |
| 普通行匹配到 | **allow**（允许） |
| `!` 开头行匹配到 | **disallow**（拒绝） |
| 空行 / `#` 开头行 | 注释，忽略 |

示例 `repos.txt`：
```txt
# 默认全部拒绝
# 1. 允许 Hana-ame 下所有仓库
Hana-ame/*
# 2. 屏蔽所有 private- 开头仓库
!Hana-ame/private-*
# 3. 特例：重新放行 private-demo（最后一条匹配成功 => allow）
Hana-ame/private-demo
```

> **支持热重载**：直接修改 `repos.txt` 保存即可生效，无需重启服务或容器。

---

## 三、如何避免 Python 网络「访问不了」

本次已在代码与部署层面对 Python 常见网络坑做了针对性加固：
1. **移除异步阻塞**：`GITHUB_TOKEN` 与 `gh auth token` 回退仅在启动时解析并缓存一次，彻底避免请求热路径阻塞事件循环导致整个服务无响应。
2. **修复 Git Smart-HTTP 代理响应头**：保留 `Content-Encoding` 与 `aiter_raw()` 一致，避免 `git clone` 收到未声明压缩的字节流而中断；同时复用 `httpx.AsyncClient` 连接池，防止 socket 泄漏。
3. **兼容代理环境**：`requirements.txt` 已内置 `httpx[socks]`，即使宿主机存在 `ALL_PROXY=socks5://...` 环境变量也不会崩溃。
4. **避免回环绑定误判**：默认监听 `0.0.0.0:8000`（Docker `BIND_HOST` 默认也是 `0.0.0.0`），并开启 Uvicorn `proxy_headers=True` 与 `forwarded_allow_ips="*"`；启动时自动打印监听地址与代理环境变量。

> **若外部仍无法访问，按顺序排查 3 项（非代码问题）**：
> 1. 宿主机防火墙 / 云厂商安全组是否放行 `8000`（或反代 `80/443`）端口。
> 2. 在本机执行 `curl -v http://127.0.0.1:8000/health` 验证进程监听正常。
> 3. 若启动日志显示 `PROXY ENV`，确认代理地址本身可达（如不需要代理，在 `.env` 或环境中清空 `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY`）。

---

## 四、部署前准备

1. **GitHub Token（`GITHUB_TOKEN`）**
   - Classic PAT：勾选 `repo`。
   - Fine-grained PAT：授予目标仓库 `Contents (R/W)`、`Pull requests (R/W)`、`Actions (Read-only)`。
2. **生成管理员密钥（`ADMIN_TOKEN`）**
   ```bash
   openssl rand -hex 32
   ```

---

## 五、方案 A：Docker 部署（推荐）

容器默认以 `read_only: true` + 内存 `tmpfs` 挂载 `/tmp` 运行，**零本地持久化目录**，仅只读挂载 `repos.txt`。

### 方式 A1：Docker Compose

```bash
# 1. 解压并进入目录
unzip git-gateway-deploy.zip && cd git-gateway

# 2. 配置环境变量与仓库规则
cp .env.example .env
chmod 600 .env
vim .env
vim repos.txt

# 3. 构建并后台启动
docker compose up -d --build

# 4. 查看启动诊断日志与状态
docker compose logs -f
docker compose ps
```

> 若前面挂了本机 Nginx/Caddy，可在 `.env` 中设置 `BIND_HOST=127.0.0.1` 避免 `8000` 端口暴露到公网。

### 方式 A2：纯 `docker run`

```bash
docker build -t git-gateway:latest .

docker run -d \
  --name git-gateway \
  --restart unless-stopped \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=512m \
  --security-opt no-new-privileges:true \
  --env-file .env \
  -e GATEWAY_HOST=0.0.0.0 \
  -e GATEWAY_PORT=8000 \
  -e REPO_RULES_FILE=/app/repos.txt \
  -p 0.0.0.0:8000:8000 \
  -v "$(pwd)/repos.txt:/app/repos.txt:ro" \
  git-gateway:latest
```

---

## 六、方案 B：直接通过 Python 3 部署

前置依赖：**Python 3.10+** 与 **Git 2.25+**。

### 方式 B1：Linux / macOS 本地直接运行

```bash
cd git-gateway
cp .env.example .env
chmod 600 .env
vim .env
vim repos.txt

# 自动建 .venv、装依赖、加载 .env 并启动（临时文件自动落到 /tmp/git-gateway）
bash deploy/run_local.sh
```

### 方式 B2：Windows 直接运行（PowerShell）

```powershell
cd git-gateway
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt

$env:GITHUB_TOKEN = "ghp_your_token"
$env:ADMIN_TOKEN = "your_admin_secret"
$env:REPO_RULES_FILE = ".\repos.txt"
$env:PUBLIC_BASE_URL = "https://proxy.com"

# 临时文件自动落到 Windows %TEMP%\git-gateway，请求结束后自动清理
python main.py --host 0.0.0.0 --port 8000
```

### 方式 B3：Linux 生产环境（systemd 守护进程）

```bash
cd git-gateway

# 1. 一键安装到 /opt/git-gateway，配置位于 /etc/git-gateway/
sudo bash deploy/install_python.sh

# 2. 编辑环境变量与白名单/黑名单
sudo vim /etc/git-gateway/env
sudo vim /etc/git-gateway/repos.txt

# 3. 启动并查看日志
sudo systemctl restart git-gateway
sudo systemctl status git-gateway
sudo journalctl -u git-gateway -f
```

> systemd unit 已启用 `ProtectSystem=strict` 与 `PrivateTmp=true`，进程对本地磁盘完全只读，仅使用私有 `/tmp`。

---

## 七、反向代理与部署后验收

反代模板见 `deploy/Caddyfile.example`（自动 HTTPS）与 `deploy/nginx.conf.example`（已配置 `client_max_body_size 12M` 与关闭缓冲）。

验收命令：
```bash
# 1. 健康检查（确认 github_token_configured=true、local_storage="none..."、repo_rules_count > 0）
curl -s http://127.0.0.1:8000/health | python3 -m json.tool

# 2. 访问允许的仓库引导页（不设 REPO_ALIASES 时直接用 /<owner>/<repo>）
curl -s http://127.0.0.1:8000/Hana-ame/repo | head -n 20

# 3. 验证未匹配/被 ! 屏蔽的仓库返回 403
curl -i http://127.0.0.1:8000/other/not-allowed

# 4. 验证只读 Clone 代理
git ls-remote http://127.0.0.1:8000/Hana-ame/repo.git
```
