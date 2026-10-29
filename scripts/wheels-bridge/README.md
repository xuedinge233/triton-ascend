# Wheels Build Bridge

A small REST service that lets external systems trigger and track
`triton-ascend` wheel builds on GitHub Actions **without ever holding a GitHub
credential**. The bridge owns a GitHub token in its own secret store; callers
authenticate with a bridge-issued API key instead.

```
external system ──(X-API-Key)──▶ bridge ──(GITHUB_TOKEN)──▶ GitHub Actions wheels.yml
                    ▲                                          │
                    └───── signed callback (wheels payload) ◀──┘ (bridge polls to completion)
```

Flow per build request:

1. Caller `POST /api/v1/wheel-builds` with build inputs (+ optional callback URL).
2. Bridge validates inputs against the `wheels.yml` schema, dispatches the
   workflow, locates the created run (the dispatch API answers `204` with no
   run id), stores it, and returns `202` with `run_id` / `run_url`.
3. A background poller watches the run. On completion it collects the wheel
   addresses (GitHub per-run artifacts + matching files in the public OBS
   nightly index) and POSTs a signed callback to the caller.
4. The caller can also poll `GET /api/v1/wheel-builds/{run_id}` at any time.

## API

All endpoints except `/healthz` require the header `X-API-Key: <key>`.

### POST /api/v1/wheel-builds → 202

Request body (all fields optional; defaults mirror the workflow):

```json
{
  "arch": "all",              // all | x86_64 | aarch64
  "python": "all",            // all | comma-separated cp310,cp311,cp312,cp313
  "branch": "all",            // all | main  (matrix branch entry to build)
  "publish": true,            // publish wheels to OBS nightly when true
  "ref": "main",              // which wheels.yml revision to dispatch against
  "callback_url": "https://your-system/hooks/wheels",
  "callback_secret": "per-request HMAC secret (optional)"
}
```

Response:

```json
{
  "run_id": 40663490101,
  "run_url": "https://github.com/triton-lang/triton-ascend/actions/runs/40663490101",
  "status": "queued",
  "inputs": {"arch": "all", "python": "all", "branch": "all", "publish": "true"},
  "ref": "main",
  "callback": {"registered": true, "signed": true}
}
```

Errors: `401` bad API key · `422` input schema violation · `400` GitHub rejected
the dispatch (bad ref/workflow/inputs) · `502` bridge token problem, or the run
did not become visible within `FIND_RUN_TIMEOUT` (dispatch may still have
succeeded — check the Actions tab).

### GET /api/v1/wheel-builds/{run_id}

Returns the stored record: `status` (`queued`/`in_progress`/`completed`/`lost`),
`conclusion`, `inputs`, `wheels` (once completed), callback state, timestamps.
`404` for runs not triggered through this bridge.

### GET /healthz

`{"ok": true, ...}` — no auth.

## Callback

When `callback_url` is given, the bridge POSTs once the run completes:

```json
{
  "event": "wheel_build.completed",
  "run_id": 40663490101,
  "run_url": "https://github.com/.../actions/runs/40663490101",
  "repo": "triton-lang/triton-ascend",
  "ref": "main",
  "status": "completed",
  "conclusion": "success",
  "completed_at": "2026-10-28T09:20:38Z",
  "inputs": {"arch": "all", "python": "all", "branch": "all", "publish": "true"},
  "wheels": {
    "github_artifacts": [
      {
        "name": "cibw-wheels-x86_64-cp312-main",
        "archive_download_url": "https://api.github.com/repos/.../artifacts/123/zip",
        "size_in_bytes": 34567890,
        "expires_at": "2027-01-26T09:00:00Z"
      }
    ],
    "obs_index_url": "https://repo.huaweicloud.com/ascend/repos/pypi/nightly/triton-ascend/",
    "obs_files": [
      "https://repo.huaweicloud.com/ascend/repos/pypi/nightly/triton-ascend/triton_ascend-3.2.1.dev20261028-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl"
    ]
  }
}
```

Notes on the two wheel sources:

- `github_artifacts` are **exact to this run** but the
  `archive_download_url` requires a GitHub token with `actions:read` on the
  repo to download, and expires 90 days after upload.
- `obs_files` are **publicly downloadable** (only meaningful when
  `publish=true` and the publish job succeeded). The OBS index accumulates
  files across builds, so the bridge matches by cp tag + arch and keeps only
  the 3 most recent build dates, newest first (max 100 entries) — it is still
  not run-scoped, so verify `conclusion` and the `.dev<YYYYMMDD>` /
  `+dev<YYYYMMDD>` build date embedded in the filenames.

Headers on the callback POST:

| Header | Value |
|---|---|
| `X-Wheels-Bridge-Event` | `wheel_build.completed` |
| `X-Wheels-Bridge-Run-Id` | run id |
| `X-Wheels-Bridge-Signature` | `sha256=<hex HMAC of the raw body>` — only when a secret is configured |

Retries: a non-2xx response or transport error is retried by the poller up to
`MAX_CALLBACK_ATTEMPTS` (default 5, one attempt per poll interval). The
callback is sent **once per completed build**; failed runs also get a callback
(`conclusion` tells you which).

Verify the signature (Python example):

```python
import hashlib, hmac

def verify(raw_body: bytes, signature_header: str, secret: str) -> bool:
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature_header)
```

## Configuration (env vars)

| Variable | Default | Meaning |
|---|---|---|
| `GITHUB_TOKEN` | *(empty)* | PAT with **Actions: Read and write** on the repo (fine-grained) or `repo` scope (classic). Required. |
| `GITHUB_REPO` | `triton-lang/triton-ascend` | owner/repo |
| `WORKFLOW_FILE` | `wheels.yml` | workflow filename |
| `DEFAULT_REF` | `main` | ref used when the request omits one |
| `BRIDGE_API_KEYS` | *(empty)* | comma-separated API keys for callers; service refuses calls until set |
| `CALLBACK_SECRET` | *(empty)* | default HMAC secret for callbacks; per-request `callback_secret` overrides |
| `OBS_INDEX_URL` | huaweicloud nightly index | public OBS index.html to match wheel files against |
| `DB_PATH` | `wheels-bridge.db` | SQLite file (persist it!) |
| `POLL_INTERVAL` | `30` | seconds between poller iterations |
| `FIND_RUN_TIMEOUT` | `60` | seconds to wait for the dispatched run to appear |
| `MAX_CALLBACK_ATTEMPTS` | `5` | callback delivery attempts |
| `GITHUB_API` | `https://api.github.com` | override for GHES |
| `LOG_LEVEL` | `INFO` | |

## Run locally

```bash
cd scripts/wheels-bridge
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
GITHUB_TOKEN=ghp_xxx \
BRIDGE_API_KEYS=dev-key-1 \
CALLBACK_SECRET=dev-callback-secret \
.venv/bin/uvicorn app:app --port 8080
```

```bash
curl -s http://127.0.0.1:8080/healthz
curl -s -X POST http://127.0.0.1:8080/api/v1/wheel-builds \
  -H 'X-API-Key: dev-key-1' -H 'Content-Type: application/json' \
  -d '{"arch":"x86_64","python":"cp312","publish":false}'
```

## Deploy (Docker)

```bash
docker build -t wheels-bridge scripts/wheels-bridge
docker run -d --name wheels-bridge -p 8080:8080 -v wheels-bridge-data:/data \
  -e GITHUB_TOKEN -e BRIDGE_API_KEYS -e CALLBACK_SECRET wheels-bridge
```

Keep the bridge **behind TLS** (reverse proxy) and reachable only by your
internal systems.

## Design & security notes

- **Single instance.** Run-discovery after dispatch is serialized by an
  in-process lock and de-duplicated against run ids already in the DB, which
  only works with one bridge instance per repo/workflow. Need more? Give each
  instance its own dispatching token (runs carry the actor) or move run
  bookkeeping to a shared store.
- **Token hygiene.** `GITHUB_TOKEN` lives only in the bridge's environment /
  secret store. Callers hold bridge API keys, which can be rotated without
  touching GitHub. Keys are compared with `hmac.compare_digest`.
- **Input validation mirrors wheels.yml.** Anything the workflow's
  `matrix-prep` would reject is rejected here with `422` before burning a run;
  `ref`/`branch` are restricted to `[A-Za-z0-9._/-]`.
- **Callback secrets.** Set `CALLBACK_SECRET` (or pass one per request). An
  unsigned callback can be spoofed by anyone who can reach the caller's
  endpoint; the bridge logs a warning when it sends one.
- **Durability.** State is in SQLite, so callbacks survive bridge restarts;
  mount `DB_PATH` on persistent storage.
- **Lost runs.** If GitHub returns 404 for a tracked run for over an hour
  (e.g. the run was deleted), it is marked `lost` instead of retried forever.
- **Concurrency warning.** `wheels.yml` uses a cancel-in-progress concurrency
  group in some configurations — dispatching a second build while one is
  running may cancel the first. Serialize triggers on the caller side if that
  matters, or check `GET /api/v1/wheel-builds/{run_id}` before dispatching.
- **Rate limits.** One authenticated dispatch + a 15-item runs list per
  trigger, one run lookup per tracked build per `POLL_INTERVAL` — negligible
  against GitHub's 5000 req/h.
