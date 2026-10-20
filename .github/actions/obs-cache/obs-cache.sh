#!/usr/bin/env bash
#
# OBS Cache — standalone script implementation (the engine behind the
# .github/actions/obs-cache composite action).
#
# Why a plain script?  On self-hosted runners where the job container's
# workspace is not actually shared with the runner's filesystem (docker-in-
# docker volume mismatch), the runner process cannot resolve local actions
# (`uses: ./.github/actions/...`) even though `actions/checkout` succeeded
# inside the container — the step fails with "Can't find 'action.yml' ...
# Did you forget to run actions/checkout?".  `run:` steps execute inside the
# container and CAN see the checkout, so workflows on such runners should
# invoke this script directly:
#
#   - name: Cache build dependencies
#     shell: bash
#     env:
#       OBS_CACHE_MODE: restore-only
#       OBS_CACHE_KEY:  my-key-${{ steps.deps.outputs.hash }}
#       OBS_CACHE_PATHS: |
#         ~/.triton/llvm
#         ${{ github.workspace }}/.ccache
#     run: bash .github/actions/obs-cache/obs-cache.sh
#
# NOTE: for the same reason, `hashFiles()` in `with:`/`env:` expressions is
# unreliable on such runners (it is evaluated runner-side) — compute key
# hashes inside a `run:` step instead.
#
# The composite action (action.yml in this directory) is a thin wrapper that
# maps its inputs onto the env vars below, so both invocation styles share
# this implementation.
#
# Environment interface (mirrors the action inputs):
#   OBS_CACHE_MODE          restore-only | save-only | both (default: both)
#   OBS_CACHE_KEY           primary cache key (required, exact match)
#   OBS_CACHE_RESTORE_KEYS  fallback keys, one per line (optional, exact
#                           match — prefix restore-keys are NOT supported)
#   OBS_CACHE_PATHS         paths to cache, one per line (required);
#                           ~ and ${GITHUB_WORKSPACE} are expanded
#   OBS_CACHE_BUCKET        default: triton-ascend-cache
#   OBS_CACHE_ENDPOINT      default: obs.cn-north-12.myhuaweicloud.com
#   OBS_CACHE_PREFIX        default: triton-ascend-cache
#   OBS_AK / OBS_SK         OBS credentials — required for save; leave
#                           unset/empty for PR-safe restore-only runs
#
# Outputs: appends `cache-hit=true|false` to $GITHUB_OUTPUT when it is set.
#
# Security model (unchanged from the action):
# - Restore (read): public OBS URLs — works on PR and push, no secrets needed.
# - Save (write): obsutil with AK/SK — callers SHOULD only pass credentials
#   on push events so that PR workflows can never write to the cache bucket.
#
# Cache object layout in OBS:
#   obs://<bucket>/<cache-prefix>/<sha256(key)>.tar.gz

set -euo pipefail

MODE="${OBS_CACHE_MODE:-both}"
KEY="${OBS_CACHE_KEY:?OBS_CACHE_KEY is required}"
RESTORE_KEYS="${OBS_CACHE_RESTORE_KEYS:-}"
CACHE_PATHS="${OBS_CACHE_PATHS:?OBS_CACHE_PATHS is required}"
BUCKET="${OBS_CACHE_BUCKET:-triton-ascend-cache}"
ENDPOINT="${OBS_CACHE_ENDPOINT:-obs.cn-north-12.myhuaweicloud.com}"
PREFIX="${OBS_CACHE_PREFIX:-triton-ascend-cache}"
AK="${OBS_AK:-}"
SK="${OBS_SK:-}"

BASE_URL="https://${BUCKET}.${ENDPOINT}/${PREFIX}"

# ---- helpers ---------------------------------------------------------------

safe_key() {
  # Produce a URL- and OBS-safe filename from an arbitrary cache key.
  echo -n "$1" | sha256sum | cut -d' ' -f1
}

expand_paths() {
  # Expand ${GITHUB_WORKSPACE} and ~ in path lines; strip trailing slashes
  # so tar behaves predictably.  Emits the paths space-separated.
  # Note: callers receive the github.workspace expression already expanded
  # by the workflow engine, so only bash vars need handling.
  local raw="$1" out="" p
  while IFS= read -r p; do
    if [ -n "${p}" ]; then
      p="${p//\$\{GITHUB_WORKSPACE\}/${GITHUB_WORKSPACE:-}}"
      p="${p//\~/${HOME}}"
      p="${p%/}"
      out="${out}${out:+ }${p}"
    fi
  done <<< "${raw}"
  echo "${out}"
}

# ---- restore ---------------------------------------------------------------

download_and_check() {
  local label="$1"   # human-readable label for logging
  local ck="$2"      # raw cache key
  local file url http_code
  file="$(safe_key "${ck}").tar.gz"
  url="${BASE_URL}/${file}"

  echo "  [${label}] ${ck}"
  echo "    URL: ${url}"

  http_code=$(curl -sS -w '%{http_code}' -o /tmp/obs-cache.tar.gz "${url}" || echo "000")

  if [ "${http_code}" = "200" ]; then
    echo "    ✓ HTTP 200 — $(du -h /tmp/obs-cache.tar.gz | cut -f1)"
    return 0
  fi

  case "${http_code}" in
    403) echo "    ✗ HTTP 403 — access denied; is the public-read policy configured for ${PREFIX}/* ?" ;;
    404) echo "    ✗ HTTP 404 — object not found in OBS" ;;
    *)   echo "    ✗ HTTP ${http_code} — download failed" ;;
  esac
  rm -f /tmp/obs-cache.tar.gz
  return 1
}

do_restore() {
  local cache_hit="false" found_key="" expanded p rk

  echo "::group::OBS cache restore"
  echo "  Bucket: ${BUCKET}.${ENDPOINT}/${PREFIX}"

  # 1) Try primary key
  if download_and_check "primary key" "${KEY}"; then
    cache_hit="true"
    found_key="${KEY}"
  fi

  # 2) Try each restore key (exact match, not prefix)
  if [ "${cache_hit}" = "false" ] && [ -n "${RESTORE_KEYS}" ]; then
    while IFS= read -r rk; do
      if [ -n "${rk}" ]; then
        if download_and_check "restore key" "${rk}"; then
          found_key="${rk}"
          break
        fi
      fi
    done <<< "${RESTORE_KEYS}"
  fi

  # 3) Extract if we got a tarball
  if [ -n "${found_key}" ]; then
    expanded=$(expand_paths "${CACHE_PATHS}")

    # Create parent directories so tar doesn't fail on missing roots.
    for p in ${expanded}; do
      mkdir -p "$(dirname "${p}")" 2>/dev/null || true
    done

    # Extract from / so absolute paths line up.
    (cd / && tar xzf /tmp/obs-cache.tar.gz 2>/dev/null) || true
    rm -f /tmp/obs-cache.tar.gz

    echo "OBS cache hit: ${found_key}"
  else
    echo "OBS cache miss — no matching key found"
  fi

  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    echo "cache-hit=${cache_hit}" >> "${GITHUB_OUTPUT}"
  fi
  echo "::endgroup::"
}

# ---- save -------------------------------------------------------------------

do_save() {
  if [ -z "${AK}" ] || [ -z "${SK}" ]; then
    echo "OBS credentials not provided — skipping cache save."
    return 0
  fi

  echo "::group::OBS cache save"

  # ---- download obsutil ----------------------------------------------------
  local arch obs_arch obsutil_url obsutil expanded existing p safe_file
  arch="$(uname -m)"
  case "${arch}" in
    x86_64)  obs_arch=amd64 ;;
    aarch64) obs_arch=arm64 ;;
    *)       echo "ERROR: unsupported arch ${arch}" >&2; exit 1 ;;
  esac

  obsutil_url="https://obs-community.obs.cn-north-1.myhuaweicloud.com/obsutil/current/obsutil_linux_${obs_arch}.tar.gz"
  curl -fL "${obsutil_url}" -o /tmp/obsutil.tar.gz
  gzip -t /tmp/obsutil.tar.gz || { echo "obsutil.tar.gz is not a valid gzip" >&2; exit 1; }
  tar xzf /tmp/obsutil.tar.gz -C /tmp
  rm -f /tmp/obsutil.tar.gz
  obsutil=$(echo /tmp/obsutil_linux_*/obsutil)
  chmod +x "${obsutil}"

  "${obsutil}" config -i="${AK}" -k="${SK}" -e="https://${ENDPOINT}"

  # ---- create tarball ------------------------------------------------------
  expanded=$(expand_paths "${CACHE_PATHS}")

  # Collect only paths that actually exist (some may be created during the
  # build and never populated; skip them to avoid tar warnings).
  existing=""
  for p in ${expanded}; do
    if [ -e "${p}" ]; then
      existing="${existing}${existing:+ }${p}"
    else
      echo "  [skip] ${p} (does not exist)"
    fi
  done

  if [ -z "${existing}" ]; then
    echo "No cache paths exist — nothing to upload"
    echo "::endgroup::"
    return 0
  fi

  safe_file="$(safe_key "${KEY}").tar.gz"
  (cd / && tar czf "/tmp/${safe_file}" ${existing})
  echo "Created /tmp/${safe_file} ($(du -h "/tmp/${safe_file}" | cut -f1))"

  # ---- upload --------------------------------------------------------------
  "${obsutil}" cp -u "/tmp/${safe_file}" "obs://${BUCKET}/${PREFIX}/${safe_file}"
  echo "Uploaded to obs://${BUCKET}/${PREFIX}/${safe_file}"

  rm -f "/tmp/${safe_file}"
  echo "::endgroup::"
}

# ---- dispatch ----------------------------------------------------------------

case "${MODE}" in
  restore-only) do_restore ;;
  save-only)    do_save ;;
  both)         do_restore; do_save ;;
  *) echo "ERROR: unknown OBS_CACHE_MODE '${MODE}' (expected restore-only|save-only|both)" >&2; exit 1 ;;
esac
