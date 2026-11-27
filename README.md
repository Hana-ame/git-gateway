# git-gateway — 零权限 AI Agent 众包中转网关

[![CI](https://github.com/Hana-ame/git-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/Hana-ame/git-gateway/actions/workflows/ci.yml)

面向「个人账号建库 + 对外零写权限 + 外部 AI Agent 通过阅读单个 URL 自助协作」场景从零重构的轻量级中转网关。


> **🚀 v3.0 Node.js 零依赖运行时（默认）**：已提供 100% 零第三方依赖的纯 Node.js（`>=18`）完整实现（`index.js` + `src/`），原生内置 `http`/`fetch`/`node:test`，无需 Python 3 与 `node_modules`，可直接 `node index.js` 启动并通过 `npm test` 跑通端到端测试（原 Python 实现已迁移到 `python` 分支，`main` 只保留 Node.js 版）。

## 核心工作流

```text
参与者的 AI Agent
  ├─ 1. GET  https://proxy.com/path/to/repo              → 读取通用说明（含要求先设 nickname）+ TODO 快照
  ├─ 2. GET  https://proxy.com/path/to/repo/todo         → 读取最新 TODO 列表（Admin 独立端点维护）
  ├─ 3. git clone https://github.com/Hana-ame/repo.git   → 优先直连 GitHub；不通则自动降级：
  │     git clone https://proxy.com/path/to/repo.git     → 只读 Clone Proxy（彻底封死 git push）
  ├─ 4. POST https://proxy.com/path/to/repo/patches      → 提交 Patch + nickname + base_branch
  │     └─ 服务端自动校验安全路径 → 在隔离 Worktree 应用 → 推送到 <nickname>/<original_branch> → 自动创建/复用 CI/CD PR
  ├─ 5. GET  https://proxy.com/path/to/repo/prs/<n>/ci   → 查询 PR 的 CI/CD 是否通过与失败 Job 列表
  └─ 6. GET  https://proxy.com/path/to/repo/prs/<n>/logs → 直接拉取失败 Job 的真实错误日志（自动去 ANSI），修复后重提 Patch
```

## 为什么必须显式传 `base_branch`？（Patch 与基准分支说明）

| 格式 | 是否包含 `base_commit` | 是否包含原始分支名 `base_branch` | 网关处理方式 |
|------|------------------------|----------------------------------|--------------|
| `git diff`（Unified Diff） | 否（仅有文件 blob 哈希） | 否 | 必须传 `base_branch`，可选传 `base_commit` |
| `git format-patch --base=<sha>` | 是（底部 `base-commit:`） | 否（多个分支可能指向同一 commit） | 必须传 `base_branch`，网关自动识别 `base-commit:` |

- **`base_branch`（必填）** 决定了两个核心语义：目标分支名 `<nickname>/<base_branch>` 以及 PR 合并目标 `base=<base_branch>`。
- **二次修复无冲突（支持增量与全量 Patch）**：若 `<nickname>/<base_branch>` 已存在，网关优先尝试增量 apply；若 Agent 提交的是基于 `base_branch` 重新生成的全量 `git diff`，网关会自动在 `base_branch` 上 apply 后用 `git commit-tree -p <old_tip>` 生成严格 **fast-forward** 提交（永不 force-push，自动复用原 PR 并重新触发 CI）。

## 相比旧版 `gitproxy` 的架构改进

1. **彻底关闭 `git-receive-pack`（只读克隆代理）**：杜绝匿名任意建分支、删分支、force-push 风险；所有写入必须经 `POST /patches` 并被服务端强制约束在 `<nickname>/<original_branch>` 命名空间下。
2. **原生 Agent 引导页 + 独立 TODO 端点**：支持路径别名映射（如 `path/to/repo -> Hana-ame/repo`），Admin 可通过带 Token 的 API 随时热更新全局/单库通用说明与 TODO 列表；内容作为**配置数据落盘**到 `CONTENT_DIR`（原子写 + `flock` 跨进程锁），容器重建/服务重启后依然存在。
3. **内置 CI/CD 真实错误日志拉取**：不仅返回 `check_runs` 状态，还直接通过 GitHub REST API 拉取失败 Job 日志（安全处理 302 跳转，不向外部 Blob 泄露 Token），自动清除 ANSI 转义符并支持 `?tail=200`。
4. **防 CI 投毒与隔离 Worktree**：默认拦截任何修改 `.github/workflows/*` 与 `.github/actions/*` 的 Patch；每次提交在临时 Worktree 中执行，失败不污染本地 Mirror。
5. **默认用 `gh` CLI 管理认证**：服务启动时调用一次 `gh auth token` 并缓存（不阻塞事件循环），`/health` 会返回 `github_token_source: "gh_cli"`；如需覆盖，显式设置 `GITHUB_TOKEN` 即可（优先级更高）。无需 SSH 密钥，纯 HTTPS + GitHub REST API v3。

## 认证方式

三种部署方式都默认复用**已登录的 `gh` CLI**，无需在配置里保存 Token：

| 部署方式 | 认证做法 |
|---------|---------|
| **Docker Compose** | 宿主机先 `gh auth login`；`docker-compose.yml` 默认把 `~/.config/gh` 只读挂载到容器内 `/home/gateway/.config/gh` |
| **Python 3 / systemd** | 安装脚本创建 `/etc/git-gateway/gh`（700）并写入 `GH_CONFIG_DIR`，执行 `sudo GH_CONFIG_DIR=/etc/git-gateway/gh gh auth login` |
| **显式覆盖** | 在 `.env` 中设置 `GITHUB_TOKEN=ghp_...`，或关闭 `USE_GH_CLI=false` |

`ADMIN_TOKEN` 用 `openssl rand -hex 32` 生成。

## 数据持久化边界

两类数据的生命周期刻意分开：

| 数据 | 存放位置 | 重启后 |
|------|---------|--------|
| Git bare clone / Worktree / Patch | 系统临时目录（`TEMP_DIR`），请求结束即删 | 不需要保留 |
| **Admin 下发的 Prompt / TODO** | **`CONTENT_DIR`**，原子写 + `flock` 锁 | **保留** |

`CONTENT_DIR` 在三种部署方式中都已配置好：Docker 用命名卷 `git-gateway-data`（挂到 `/var/lib/git-gateway`），systemd 用 `StateDirectory=git-gateway`，本地开发留空则退回内存态（重启即失，启动日志会告警）。

随时可用 `/health` 确认当前模式：

```bash
curl -s http://127.0.0.1:8000/health | jq .content_store
# {"mode":"file","path":"/var/lib/git-gateway/content/content.json","degraded_reason":null,...}
```

若 `mode` 为 `memory`，说明目录不可写（常见于 `read_only` 容器忘记挂卷），此时 Admin 内容会在重启后回到内置默认模板。

## 快速启动

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 认证：默认走已登录的 gh CLI（也可用 GITHUB_TOKEN 覆盖）
gh auth login

export ADMIN_TOKEN="$(openssl rand -hex 32)"
export PUBLIC_BASE_URL="https://proxy.com"
# 白名单：gitignore 风格规则文件（支持热重载）；旧写法 ALLOWED_REPOS="Hana-ame/repo" 仍兼容
export REPO_RULES_FILE="./repos.txt"
export REPO_ALIASES="path/to/repo=Hana-ame/repo"

python main.py --host 0.0.0.0 --port 8000
```

## API 端点一览

### 公开端点（Agent 零凭据调用）

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/<repo_path>` | Agent 引导页（通用说明 + TODO 快照；默认 Markdown，`?format=json` 返回 JSON） |
| `GET` | `/<repo_path>/todo` | 仓库 TODO 列表（默认 Markdown，`?format=json` 返回 JSON） |
| `GET/POST` | `/<repo_path>[.git]/info/refs`, `/git-upload-pack` | 只读 Git Smart-HTTP Clone 代理（`git-receive-pack` 一律 403） |
| `POST` | `/<repo_path>/patches` | 提交 Patch（支持 raw body + query 或 JSON），自动推送到 `<nickname>/<base_branch>` 并开/复用 PR |
| `GET` | `/<repo_path>/prs` | 列出 PR（支持 `?nickname=&base_branch=&state=open\|closed\|all`） |
| `GET` | `/<repo_path>/prs/<n>/ci` | 查询 PR 的 CI/CD 汇总状态（`passed`/`failed`/`running`/`none`）与失败 Job 列表 |
| `GET` | `/<repo_path>/prs/<n>/logs` | 拉取 PR 失败 Job 的真实错误日志（默认纯文本，`?tail=200`、`?all=1`、`?format=json`） |
| `GET` | `/<repo_path>/actions/jobs/<job_id>/logs` | 拉取指定 Job ID 的错误日志 |
| `GET` | `/repos` | 只读仓库目录（返回当前生效规则允许的具体仓库列表与别名，供前端看板自动拉取） |
| `GET` | `/health` | 服务健康检查 |

### 管理员端点（需 `Authorization: Bearer <ADMIN_TOKEN>`）

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET / PUT` | `/admin/prompt` | 查看 / 更新全局默认通用说明（支持模板变量 `{{OWNER}}`、`{{REPO}}`、`{{GITHUB_URL}}`、`{{CLONE_PROXY_URL}}`、`{{TODO_URL}}`、`{{PATCH_URL}}`、`{{PRS_URL}}`） |
| `GET / PUT / DELETE` | `/admin/<repo_path>/prompt` | 查看 / 覆盖 / 删除单仓库专属通用说明 |
| `GET / PUT` | `/admin/<repo_path>/todo` | 查看 / 更新该仓库的 TODO 列表（支持 raw Markdown 或 JSON `{"content": "..."}`） |

#### 管理员更新示例

```bash
# 更新全局通用说明
curl -X PUT "https://proxy.com/admin/prompt" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  --data-binary @prompt.md

# 更新某仓库的 TODO 列表
curl -X PUT "https://proxy.com/admin/path/to/repo/todo" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  --data-binary @TODO.md
```

## 运行测试

```bash
python3 -m unittest discover -s tests -v
```
