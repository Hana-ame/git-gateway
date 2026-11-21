# git-gateway 测试报告

- **测试来源**：GitHub Actions CI（[`.github/workflows/ci.yml`](.github/workflows/ci.yml)），commit `305bbe9`
- **运行环境**：`Ubuntu 26.04 (ubuntu-26.04)` / Python `3.11` 与 `3.12` 双版本矩阵 / `git 2.56.1`
- **测试结果**：**7 / 7 通过**（两个 Python 版本均通过）
- **语句覆盖率**：`78%`（`gateway/` 共 1127 条语句，未覆盖 243 条）
- **依赖**：无网络依赖——所有用例在本地真实裸 Git 仓库 + 本地 Mock GitHub HTTP 服务上端到端执行

## 测试用例明细

| 用例名 | 验证范围 |
|--------|----------|
| `test_01_landing_page_todo_and_admin_persistence` | Agent 引导页渲染、路径别名与直连路由、JSON/Markdown 内容协商、Admin 鉴权 (401)、Prompt/TODO 更新（v2 起为进程内存态，不再跨重启持久化） |
| `test_02_readonly_clone_proxy_allows_fetch_blocks_push` | 只读 Git Smart-HTTP 克隆代理放行 `git-upload-pack`，彻底拦截 GET/POST `git-receive-pack` (403) |
| `test_03_patch_lifecycle_resubmission_noop_and_ci_logs` | Patch 提交（含 CRLF/缺尾换行规范化）、自动推至 `<nick>/<branch>` 并开 PR、重复提交 noop 检测、CI 失败日志拉取（去 ANSI、tail 截断、跳转 Blob 时不泄露 Token）、全量二次修复严格 fast-forward 复用原 PR、running/passed/failed/none 状态机 |
| `test_04_format_patch_nested_branch_and_file_ops` | 嵌套分支名 (`bob/feat/ui`) 隔离推送、`git format-patch --base=<sha>` 自动识别 base-commit、新建文件提交验证 |
| `test_05_security_and_validation_guardrails` | CI 投毒拦截（`.github/workflows/*` 与 `.github/actions/*` 均 403）、保留昵称与非法分支名拦截 (400)、非白名单仓库拦截 (403)、超大 Patch 拦截 (413) |
| `test_06_multithreaded_concurrency_stress` | 12 线程池 14 项并发压测（8 个不同 Agent 同时提交 + 3 个相同昵称并发提交 + 3 个 Admin TODO 并发写入）：验证 100% 成功、同分支 3 次提交无丢写、零残留临时 Worktree |
| `test_07_gitignore_style_repo_rules_and_hot_reload` | v2 新增：gitignore 风格规则匹配（`!` 取反、通配符、尾斜杠简写、最后匹配生效、默认拒绝）与规则文件热重载 |

## 并发与安全机制验证结论

- **并发安全**：`GitPatchEngine` 使用按仓库粒度隔离的进程内 `threading.Lock` 与跨进程锁文件，每次 Patch 在独立 `mkdtemp` 临时 Worktree 中执行并在 `finally` 中移除并执行 `worktree prune`。压测中 14 个并发请求（含同一昵称 `shared-bot/main` 的 3 次并发写入）全部返回 `200 OK`，上游裸库精确记录全部 8 个独立分支与 3 次顺序 fast-forward 提交，Mirror 仅保留主裸库、无残留 Worktree。`ContentStore` 自 v2 起为进程内内存态，并发写入由 `threading.Lock` 保护（多副本部署时各副本的 Admin 内容相互独立）。
- **防 CI 投毒与凭据隔离**：修改 `.github/workflows/*`、`./.github/workflows/*`、`.github/actions/*` 均在触碰 Git 前返回 `403`；拉取 GitHub Actions 日志遇 302 跳转至外部预签名 Blob 时已验证不附带 `Authorization` 请求头。
- **零本地持久化**（v2）：所有 bare clone / worktree / patch 文件均落在 `TEMP_DIR`，请求结束时删除；`ContentStore` 不写磁盘。容器以 `read_only: true` + `tmpfs /tmp` 运行。

## CI 流水线覆盖范围

| Job | 内容 |
|-----|------|
| `Syntax`（3.11 / 3.12） | `compileall -W error::SyntaxWarning`、全模块导入检查、AST 扫描 `raise` 之后的不可达语句 |
| `Test`（3.11 / 3.12） | pytest 全量用例 + 覆盖率报告、`repos.txt` 规则解析与 allow/deny 语义断言 |
| `Docker build + smoke test` | `docker compose config` 校验、镜像构建、以 `--read-only --tmpfs /tmp --security-opt no-new-privileges` 启动并探测 `/health`、确认以 `uid=1000(gateway)` 非 root 运行 |
| `Deploy assets sanity` | 部署文件完整性、`.env.example` 覆盖全部 `getenv` 变量、systemd unit 语法校验、shell 脚本可执行位与语法 |
