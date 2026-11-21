# git-gateway — 零权限 AI Agent 众包中转网关

[![CI](https://github.com/Hana-ame/git-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/Hana-ame/git-gateway/actions/workflows/ci.yml)

面向「个人账号建库 + 对外零写权限 + 外部 AI Agent 通过阅读单个 URL 自助协作」场景从零重构的轻量级中转网关。

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
2. **原生 Agent 引导页 + 独立 TODO 端点**：支持路径别名映射（如 `path/to/repo -> Hana-ame/repo`），Admin 可通过带 Token 的 API 随时热更新全局/单库通用说明与 TODO 列表；内容仅存于进程内存，**不落任何本地磁盘**（重启后回到默认模板，可用 Admin API 重新下发）。
3. **内置 CI/CD 真实错误日志拉取**：不仅返回 `check_runs` 状态，还直接通过 GitHub REST API 拉取失败 Job 日志（安全处理 302 跳转，不向外部 Blob 泄露 Token），自动清除 ANSI 转义符并支持 `?tail=200`。
4. **防 CI 投毒与隔离 Worktree**：默认拦截任何修改 `.github/workflows/*` 与 `.github/actions/*` 的 Patch；每次提交在临时 Worktree 中执行，失败不污染本地 Mirror。
5. **零 `gh` CLI / SSH 依赖**：纯 `GITHUB_TOKEN` + HTTPS + GitHub REST API v3，支持 Docker / systemd 一键部署。

## 快速启动

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export GITHUB_TOKEN="ghp_your_token_with_repo_and_actions_read"
export ADMIN_TOKEN="your_strong_admin_secret"
export PUBLIC_BASE_URL="https://proxy.com"
# 白名单：gitignore 风格规则文件（支持热重载）；也可用旧写法 ALLOWED_REPOS="Hana-ame/repo"
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
