# CI Workflows 总览

本目录下 GitHub Actions workflow 的清单：名称、作用、运行机器、运行条件、频率与执行时间。

运行机器只分两类：**self-hosted**（自托管，标签 `linux-amd64-cpu-16`、`linux-aarch64-cpu-16`、`linux-aarch64-cpu-1`、`linux-aarch64-a2-1` 及动态分配的 NPU runner）与 **GitHub-hosted**（`ubuntu-latest` / `ubuntu-22.04`）。

## 1. Self-hosted（自托管）

| 名称 (文件) | 作用 | 运行机器 | 运行条件 | 频率 | 执行时间 |
|---|---|---|---|---|---|
| **LLVM Build** (`llvm-build.yml`) - 当前已关闭任务触发 | 编译 LLVM/MLIR 产物，上传 OBS | self-hosted（迁移中） | push/PR 改动 `cmake/llvm-hash.txt`、`llvm_patch/**`、本 workflow；+ 手动 | 很少（仅 LLVM 升级时） | 约 0.5–2h |
| **Wheels** (`wheels.yml`) - 当前已关闭任务触发 | 夜间构建 manylinux wheel + auditwheel，上传 OBS nightly | self-hosted（迁移中） | 定时 + 手动 + PR（仅改 `wheels.yml`） | 每日 08:00 UTC | 约 1.5–2h |
| **Release Triton Ascend Images** (`build-docker-image.yml`) - 当前已关闭任务触发 | 构建并推送多架构 CANN 镜像到 quay.io | self-hosted（迁移中） | push `main` + PR `main`，仅当 `Dockerfile`/`Makefile`/`requirements*`/本 workflow 变更 | 较少 | 约 10–30m |
| **Ascend950 Wheels Build** (`Ascend950-wheels-build.yml`) | PR/push 的 triton-ascend wheel 构建（x86，供下游流水线） | self-hosted | PR + push `main`/`release/**`（忽略 docs 等路径） | 每次相关 PR/push | 约 10–20m |
| **Integration Tests NPU** (`integration-tests-ascend.yml`) | 在 NPU 上 build + 跑单测 / MLIR FileCheck | self-hosted（NPU，动态矩阵） | `workflow_call`（被 `ci.yml` 调用） | 跟随 ci | 约 10–20m |
| **Ascend950 Pipeline Tests** (`Ascend950-pipeline-tests.yml`) | Wheels 完成后：上传 OBS → 触发蓝黄跨域流水线并轮询 | self-hosted（finalize 子任务在 GitHub-hosted） | `workflow_run`：在 "Ascend950 Wheels Build" 成功完成后 | 每次 Ascend950 wheels build 完成 | 轮询上限 ~6h（需等待外部流水线，正常约 0.5–2h） |
| **Documentation** (`documentation.yml`) | 构建/发布文档 | self-hosted | 定时 + 手动 | 每日 00:00 UTC |  <1m |

## 2. GitHub-hosted（GitHub 自带机器）

| 名称 (文件) | 作用 | 运行机器 | 运行条件 | 频率 | 执行时间 |
|---|---|---|---|---|---|
| **Integration Tests** (`ci.yml`) | 主编排器：调用 runner-preparation → integration-tests-ascend | 无自身 runner（编排，调用 reusable） | PR（忽略 docs/构建类路径）+ push `main`/`release/**` + `merge_group` + 手动 | 每次 PR/push | 即时（仅分发） |
| **Runner Preparation** (`runner-preparation.yml`) | 探测可用 NPU 资源，生成测试矩阵 | GitHub-hosted | `workflow_call` | 跟随 ci | ～10s |
| **Pre-Commit Check** (`pre-commit.yml`) | 代码格式 / lint | GitHub-hosted | PR + push `main`/`release/**` + `merge_group` + 手动 | 每次 PR/push | ~1–2m |
| **PR Title Check** (`pr-title-check.yml`) | 校验 PR 标题格式 / 长度 | GitHub-hosted | PR opened/edited/reopened/synchronize | 每次 PR 改标题 | ～10s |
| **PR Labeler & Codeowner Notify** (`labeler.yml`) | 自动打标签 + 通知 codeowner | GitHub-hosted | `pull_request_target` opened/sync/reopen/edited | 每次 PR | <1m |
| **Restricted Files Labeler** (`protected-files-check.yml`) | 标记改动受限路径的 PR | GitHub-hosted | `pull_request_target` | 每次 PR | ～10s |
| **Issue auto-labeling** (`issue-labeler.yml`) | 按标题/正文给 issue 打标签 | GitHub-hosted | issues opened/edited | 每次 issue | ～10s |
| **Rebuild / Retry Tasks** (`rebuild-tasks.yml`) | PR 评论 ChatOps `/rebuild` `/retry` | GitHub-hosted | `issue_comment` created | 按需 | ～10s |
| **Create Release** (`create_release.yml`) | 打 sdist 源码包、发 GitHub Release | GitHub-hosted | push `main`/`release/*`/`v*` tag、release published、PR 改本文件 | 发版时 | ~1–2m |
| **Stale issue & PR cleanup** (`stale.yml`) | 关闭陈旧 issue/PR | GitHub-hosted | 定时 + 手动 | 每日 03:37 UTC | ~1m |
| **Sync Branches** (`sync-branches.yml`) | 在两分支间开同步 PR | GitHub-hosted | 仅手动（输入 source/target 分支） | 按需 | ~1m |

## 小结

- **定时任务**：Wheels（每日 08:00 UTC）、Documentation（每日 00:00 UTC）、Stale（每日 03:37 UTC）。
- **Self-hosted（重型 / NPU）**：LLVM Build、Wheels、Release Images、Ascend950 Wheels、Integration Tests NPU、Ascend950 Pipeline Tests、Documentation；其余均为 GitHub-hosted，多为秒级到几分钟。
- **reusable / 编排**：`ci.yml`（主入口）→ `runner-preparation.yml` + `integration-tests-ascend.yml`。
