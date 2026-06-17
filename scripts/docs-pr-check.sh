#!/usr/bin/env bash
# Docs PR gate wrapper around the openEuler docs checker (docs-ci-v2.js).
#
# Runs the documentation checker against the .md / .mdx / _toc.yaml files
# changed relative to a base ref, then turns its output.md report into a
# pass/fail exit code. This wrapper exists because the checker always exits 0
# and only signals the result via output.md (a failing report starts with the
# "❌" marker), so the gate logic has to live outside the bundle.
#
# The checker itself is NOT vendored into this repo. It is a prebuilt bundle
# published on the `ci` branch of openeuler/docs-website; we fetch it at run
# time (or reuse a local copy if one is already present next to this repo).
#
# The check rules are NOT defined here either: they live in the central, shared
# config at gitcode Ascend/docs/.doctools/config.json, which already has an
# entry for Ascend/triton-ascend. We only point the checker at that config.
#
# Usage:
#   scripts/docs-pr-check.sh [base-ref]
#
# Environment overrides:
#   DOCS_BASE_REF       base ref to diff against (default: $1, else origin/main)
#   DOCS_TARGET_REPO    owner/repo key for CI config lookup. MUST be the gitcode
#                       name (Ascend/triton-ascend), not the GitHub repo name,
#                       or the central config entry won't match.
#   DOCS_CI_CONFIG      CI config path or URL (default: central gitcode config)
#   DOCS_CLI            path to docs-ci-v2.js; if it exists it is used as-is,
#                       otherwise the checker is downloaded to this path
#                       (default: <repo-root>/.cache/docs-ci-v2.js)
#   DOCS_CLI_URL        download source for the checker (default: official ci branch)
#   DOCS_CLI_SHA256     optional expected sha256 of the checker; verified if set
#   DOCS_OUTPUT_COUNT   max errors listed in output.md (default: 50)
#   DOCS_OUTPUT_PATH    output report path (default: <repo-root>/output.md)
#   DOCS_CHECK_ALL      set to "true" to check all docs, not just changed ones

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"

BASE_REF="${DOCS_BASE_REF:-${1:-origin/main}}"
TARGET_REPO="${DOCS_TARGET_REPO:-Ascend/triton-ascend}"
CI_CONFIG="${DOCS_CI_CONFIG:-https://raw.gitcode.com/Ascend/docs/raw/master/.doctools/config.json}"
CLI_URL="${DOCS_CLI_URL:-https://raw.gitcode.com/openeuler/docs-website/raw/ci/docs-ci-v2.js}"
OUTPUT_COUNT="${DOCS_OUTPUT_COUNT:-50}"
OUTPUT_PATH="${DOCS_OUTPUT_PATH:-$REPO_ROOT/output.md}"

# Resolve the checker: prefer an explicit/local copy, otherwise download it.
CLI_PATH="${DOCS_CLI:-$REPO_ROOT/.cache/docs-ci-v2.js}"
if [ ! -f "$CLI_PATH" ] && [ -f "$REPO_ROOT/docs-ci-v2.js" ]; then
  CLI_PATH="$REPO_ROOT/docs-ci-v2.js"
fi
if [ ! -f "$CLI_PATH" ]; then
  echo "Fetching docs checker from $CLI_URL"
  mkdir -p "$(dirname "$CLI_PATH")"
  curl -fsSL --retry 3 --max-time 120 "$CLI_URL" -o "$CLI_PATH"
fi

if [ -n "${DOCS_CLI_SHA256:-}" ]; then
  actual="$(shasum -a 256 "$CLI_PATH" | cut -d' ' -f1)"
  if [ "$actual" != "$DOCS_CLI_SHA256" ]; then
    echo "::error::docs checker sha256 mismatch: expected $DOCS_CLI_SHA256, got $actual"
    exit 1
  fi
fi

ARGS=(
  --repoPath="$REPO_ROOT"
  --targetOwnerRepo="$TARGET_REPO"
  --targetBranch="$BASE_REF"
  --ciConfigUrl="$CI_CONFIG"
  --outputCount="$OUTPUT_COUNT"
  --outputPath="$OUTPUT_PATH"
)
if [ "${DOCS_CHECK_ALL:-}" = "true" ]; then
  ARGS+=(--checkAll=true)
fi

rm -f "$OUTPUT_PATH"
node "$CLI_PATH" "${ARGS[@]}"

if [ ! -f "$OUTPUT_PATH" ]; then
  echo "::error::docs checker produced no report at $OUTPUT_PATH"
  exit 1
fi

if head -n 1 "$OUTPUT_PATH" | grep -q "❌"; then
  echo "::error::Docs PR gate failed - see report below / job summary."
  cat "$OUTPUT_PATH"
  exit 1
fi

echo "Docs PR gate passed."
