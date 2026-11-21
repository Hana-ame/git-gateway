# git-gateway 测试报告

> **版本说明**：下表为 v1（文件持久化架构）的快照数据。v2 起 `ContentStore` 改为纯进程内存、`GitPatchEngine` 全部落在 `TEMP_DIR` 并在请求结束时删除，同时新增 `test_07_gitignore_style_repo_rules_and_hot_reload`（gitignore 风格规则匹配与热重载）。用例总数由 6 增至 7，**权威结果以 GitHub Actions CI 为准**。

- **测试时间 (UTC)**：2026-11-21 16:11:27 UTC（v1 快照）
- **运行环境**：`Linux 6.12.105+ (x86_64)` / Python `3.11.2` / `git version 2.39.5`
- **测试结果**：**6 / 6 通过**（v1；失败：0，错误：0，总耗时：`5074.4 ms`）

## 测试用例明细

| 用例名 | 状态 | 耗时 (ms) | 验证范围 |
|--------|------|----------:|----------|
| `test_01_landing_page_todo_and_admin_persistence` | **PASS** | 554.7 | Agent 引导页渲染、路径别名与直连路由、JSON/Markdown 协商、Admin 鉴权 (401)、Prompt/TODO 更新（v1 为跨重启磁盘持久化，v2 改校验同实例内存态） |
| `test_02_readonly_clone_proxy_allows_fetch_blocks_push` | **PASS** | 587.6 | 只读 Git Smart-HTTP 克隆代理放行 git-upload-pack，彻底拦截 GET/POST git-receive-pack (403) |
| `test_03_patch_lifecycle_resubmission_noop_and_ci_logs` | **PASS** | 1294.8 | Patch 提交（含 CRLF/缺尾换行规范化）、自动推至 <nick>/<branch> 与开 PR、重复提交 noop 检测、CI 失败日志拉取（去 ANSI、tail 截断、防 Blob 泄露 Token）、全量二次修复严格 fast-forward 复用原 PR、running/passed/none 状态机 |
| `test_04_format_patch_nested_branch_and_file_ops` | **PASS** | 789.1 | 嵌套分支名 (bob/feat/ui) 隔离推送、git format-patch --base=<sha> 自动识别 base-commit、新建文件提交验证 |
| `test_05_security_and_validation_guardrails` | **PASS** | 552.8 | CI 投毒拦截 (.github/workflows/* 与 .github/actions/* 均 403)、保留昵称与非法分支名拦截 (400)、非白名单仓库拦截 (403)、超大 Patch 拦截 (413) |
| `test_06_multithreaded_concurrency_stress` | **PASS** | 1294.5 | 12 线程池 14 项并发压测（8 个不同 Agent 同时提交 + 3 个相同昵称并发提交 + 3 个 Admin TODO 并发写入）：验证 100% 成功、同分支 3 次提交无丢写、零残留临时 Worktree |

## 并发与安全机制验证结论

- **线程与多进程并发安全**：`GitPatchEngine` 使用按仓库粒度隔离的进程内 `threading.Lock` 与跨进程锁文件，每次 Patch 在独立 `mkdtemp` 临时 Worktree 中执行并在 `finally` 中移除与 `worktree prune`。压测中 14 个并发请求（含同一昵称 `shared-bot/main` 的 3 次并发写入）全部返回 `200 OK`，上游裸库精确记录全部 8 个独立分支与 3 次顺序 fast-forward 提交，Mirror 仅保留主裸库无残留 Worktree。`ContentStore` 自 v2 起为进程内内存态，并发写入由 `threading.Lock` 保护（多副本部署时各副本的 Admin 内容相互独立）。
- **防 CI 投毒与凭据隔离**：修改 `.github/workflows/*`、`./.github/workflows/*`、`.github/actions/*` 均在触碰 Git 前返回 `403`；拉取 GitHub Actions 日志 302 跳转至外部预签名 Blob 时已验证不附带 `Authorization` 请求头。
- **无网络依赖**：所有测试均在本地真实裸 Git 仓库 + 本地 Mock GitHub HTTP 服务上端到端执行。
