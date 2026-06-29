# CI Workflows Overview

An inventory of the GitHub Actions workflows in this directory: name, purpose, runner, trigger conditions, frequency, and execution time.

Runners are split into just two classes: **self-hosted** (labels `linux-amd64-cpu-16`, `linux-aarch64-cpu-16`, `linux-aarch64-cpu-1`, `linux-aarch64-a2-1`, plus dynamically allocated NPU runners) and **GitHub-hosted** (`ubuntu-latest` / `ubuntu-22.04`).

## 1. Self-hosted

| Name (file) | Purpose | Runner | Trigger conditions | Frequency | Execution time |
|---|---|---|---|---|---|
| **LLVM Build** (`llvm-build.yml`) — triggers currently disabled | Compile LLVM/MLIR artifacts, upload to OBS | self-hosted (migrating) | push/PR touching `cmake/llvm-hash.txt`, `llvm_patch/**`, this workflow; + manual | Rare (only on LLVM bumps) | ~0.5–2h |
| **Wheels** (`wheels.yml`) — triggers currently disabled | Nightly manylinux wheel build + auditwheel, upload to OBS nightly | self-hosted (migrating) | schedule + manual + PR (only when `wheels.yml` changes) | Daily 08:00 UTC | ~1.5–2h |
| **Release Triton Ascend Images** (`build-docker-image.yml`) — triggers currently disabled | Build and push multi-arch CANN images to quay.io | self-hosted (migrating) | push `main` + PR `main`, only when `Dockerfile`/`Makefile`/`requirements*`/this workflow change | Infrequent | ~10–30m |
| **Ascend950 Wheels Build** (`Ascend950-wheels-build.yml`) | triton-ascend wheel build for PR/push (x86, feeds the downstream pipeline) | self-hosted | PR + push `main`/`release/**` (ignoring docs etc.) | Every related PR/push | ~10–20m |
| **Integration Tests NPU** (`integration-tests-ascend.yml`) | Build on NPU + run unit tests / MLIR FileCheck | self-hosted (NPU, dynamic matrix) | `workflow_call` (invoked by `ci.yml`) | Follows ci | ~10–20m |
| **Ascend950 Pipeline Tests** (`Ascend950-pipeline-tests.yml`) | After Wheels completes: upload to OBS → trigger the blue/yellow cross-region pipeline and poll | self-hosted (the finalize sub-job runs on GitHub-hosted) | `workflow_run`: after "Ascend950 Wheels Build" completes successfully | Every time the Ascend950 wheels build completes | Polling cap ~6h (waits on the external pipeline; normal ~0.5–2h) |
| **Documentation** (`documentation.yml`) | Build/publish docs | self-hosted | schedule + manual | Daily 00:00 UTC | <1m |

## 2. GitHub-hosted

| Name (file) | Purpose | Runner | Trigger conditions | Frequency | Execution time |
|---|---|---|---|---|---|
| **Integration Tests** (`ci.yml`) | Main orchestrator: calls runner-preparation → integration-tests-ascend | None of its own (orchestrator, calls reusables) | PR (ignoring docs/build-type paths) + push `main`/`release/**` + `merge_group` + manual | Every PR/push | Instant (dispatch only) |
| **Runner Preparation** (`runner-preparation.yml`) | Probe available NPU resources, generate the test matrix | GitHub-hosted | `workflow_call` | Follows ci | ~10s |
| **Pre-Commit Check** (`pre-commit.yml`) | Code formatting / lint | GitHub-hosted | PR + push `main`/`release/**` + `merge_group` + manual | Every PR/push | ~1–2m |
| **PR Title Check** (`pr-title-check.yml`) | Validate PR title format / length | GitHub-hosted | PR opened/edited/reopened/synchronize | Every PR title change | ~10s |
| **PR Labeler & Codeowner Notify** (`labeler.yml`) | Auto-label + notify codeowners | GitHub-hosted | `pull_request_target` opened/sync/reopen/edited | Every PR | <1m |
| **Restricted Files Labeler** (`protected-files-check.yml`) | Flag PRs touching restricted paths | GitHub-hosted | `pull_request_target` | Every PR | ~10s |
| **Issue auto-labeling** (`issue-labeler.yml`) | Label issues by title/body | GitHub-hosted | issues opened/edited | Every issue | ~10s |
| **Rebuild / Retry Tasks** (`rebuild-tasks.yml`) | PR-comment ChatOps `/rebuild` `/retry` | GitHub-hosted | `issue_comment` created | On demand | ~10s |
| **Create Release** (`create_release.yml`) | Build the sdist, publish a GitHub Release | GitHub-hosted | push `main`/`release/*`/`v*` tag, release published, PR touching this file | At release time | ~1–2m |
| **Stale issue & PR cleanup** (`stale.yml`) | Close stale issues/PRs | GitHub-hosted | schedule + manual | Daily 03:37 UTC | ~1m |
| **Sync Branches** (`sync-branches.yml`) | Open a sync PR between two branches | GitHub-hosted | Manual only (source/target branch inputs) | On demand | ~1m |

## Summary

- **Scheduled jobs**: Wheels (daily 08:00 UTC), Documentation (daily 00:00 UTC), Stale (daily 03:37 UTC).
- **Self-hosted (heavy / NPU)**: LLVM Build, Wheels, Release Images, Ascend950 Wheels, Integration Tests NPU, Ascend950 Pipeline Tests, Documentation; everything else is GitHub-hosted and finishes in seconds to a few minutes.
- **Reusable / orchestration**: `ci.yml` (main entry) → `runner-preparation.yml` + `integration-tests-ascend.yml`.
