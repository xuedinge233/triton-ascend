"""Wheels build bridge — REST interface for external systems.

Lets an external system trigger triton-ascend wheel builds on GitHub Actions
(and track them to completion) WITHOUT the caller ever holding a GitHub
credential. The bridge owns a GitHub PAT (Actions: Read and write on the
repo) in its own secret store, dispatches .github/workflows/wheels.yml,
polls the run to completion, and POSTs a signed callback carrying the wheel
artifact addresses (per-run GitHub artifacts + public OBS nightly files).

Endpoints:
  POST /api/v1/wheel-builds          trigger a build (returns run id/url)
  GET  /api/v1/wheel-builds/{run_id} query status + wheel addresses
  GET  /healthz                      liveness

See README.md for the full contract, deployment and security notes.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, field_validator

log = logging.getLogger("wheels-bridge")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# --------------------------------------------------------------------------
# Configuration (env)
# --------------------------------------------------------------------------
GITHUB_API = os.environ.get("GITHUB_API", "https://api.github.com").rstrip("/")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "triton-lang/triton-ascend")
WORKFLOW_FILE = os.environ.get("WORKFLOW_FILE", "wheels.yml")
WORKFLOW_PATH = f".github/workflows/{WORKFLOW_FILE}"
DEFAULT_REF = os.environ.get("DEFAULT_REF", "main")
API_KEYS = [k.strip() for k in os.environ.get("BRIDGE_API_KEYS", "").split(",") if k.strip()]
CALLBACK_SECRET = os.environ.get("CALLBACK_SECRET", "")
DB_PATH = os.environ.get("DB_PATH", "wheels-bridge.db")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "30"))
FIND_RUN_TIMEOUT = int(os.environ.get("FIND_RUN_TIMEOUT", "60"))
MAX_CALLBACK_ATTEMPTS = int(os.environ.get("MAX_CALLBACK_ATTEMPTS", "5"))
# Public index of the OBS nightly dir that Publish-Wheels uploads to and
# regenerates after every publish (index.html lists every *.whl present).
OBS_INDEX_URL = os.environ.get(
    "OBS_INDEX_URL",
    "https://repo.huaweicloud.com/ascend/repos/pypi/nightly/triton-ascend/index.html",
)

VALID_ARCH = {"all", "x86_64", "aarch64"}
VALID_PYTHON = {"cp310", "cp311", "cp312", "cp313"}
REF_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_ago(seconds: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Request models (mirror the wheels.yml workflow_dispatch input schema)
# --------------------------------------------------------------------------
class TriggerRequest(BaseModel):
    arch: str = "all"
    python: str = "all"
    branch: str = "all"
    publish: bool = True
    # Which wheels.yml revision to dispatch against (also the checkout base);
    # defaults to DEFAULT_REF. The `branch` input above selects the matrix
    # branch entry to BUILD — they are independent knobs.
    ref: str | None = None
    callback_url: str | None = None
    # Per-request HMAC secret for signing callbacks; falls back to the
    # bridge-wide CALLBACK_SECRET when omitted.
    callback_secret: str | None = None

    @field_validator("arch")
    @classmethod
    def _arch(cls, v: str) -> str:
        if v not in VALID_ARCH:
            raise ValueError(f"arch must be one of {sorted(VALID_ARCH)}")
        return v

    @field_validator("python")
    @classmethod
    def _python(cls, v: str) -> str:
        if v == "all":
            return v
        parts = [p.strip() for p in v.split(",") if p.strip()]
        if not parts:
            raise ValueError("python must be 'all' or a comma-separated list")
        bad = [p for p in parts if p not in VALID_PYTHON]
        if bad:
            raise ValueError(f"unknown python version(s) {bad}; valid: {sorted(VALID_PYTHON)}")
        return ",".join(parts)

    @field_validator("branch")
    @classmethod
    def _branch(cls, v: str) -> str:
        if v != "all" and not REF_RE.match(v):
            raise ValueError("branch contains invalid characters")
        return v

    @field_validator("ref")
    @classmethod
    def _ref(cls, v: str | None) -> str | None:
        if v is not None and not REF_RE.match(v):
            raise ValueError("ref contains invalid characters")
        return v

    @field_validator("callback_url")
    @classmethod
    def _cb(cls, v: str | None) -> str | None:
        if v is not None and not re.match(r"^https?://", v):
            raise ValueError("callback_url must be http(s)")
        return v


# --------------------------------------------------------------------------
# SQLite store (stdlib; survives restarts so callbacks are never lost)
# --------------------------------------------------------------------------
class Store:
    def __init__(self, path: str):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS builds (
              run_id            INTEGER PRIMARY KEY,
              repo              TEXT NOT NULL,
              ref               TEXT NOT NULL,
              inputs_json       TEXT NOT NULL,
              callback_url      TEXT,
              callback_secret   TEXT,
              status            TEXT NOT NULL,
              conclusion        TEXT,
              run_url           TEXT,
              wheels_json       TEXT,
              callback_state    TEXT NOT NULL DEFAULT 'none',
              callback_attempts INTEGER NOT NULL DEFAULT 0,
              created_at        TEXT NOT NULL,
              updated_at        TEXT NOT NULL,
              completed_at      TEXT
            )
        """)
        self._db.commit()

    def _exec(self, sql: str, args: tuple = ()):
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur

    def insert(self, *, run_id: int, ref: str, inputs: dict, callback_url: str | None,
               callback_secret: str | None, status: str, run_url: str) -> None:
        now = utcnow()
        self._exec(
            """INSERT INTO builds (run_id, repo, ref, inputs_json, callback_url,
                   callback_secret, status, run_url, callback_state, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, GITHUB_REPO, ref, json.dumps(inputs), callback_url, callback_secret,
             status, run_url, "pending" if callback_url else "none", now, now),
        )

    def known_run_ids(self) -> set[int]:
        with self._lock:
            rows = self._db.execute("SELECT run_id FROM builds").fetchall()
        return {r["run_id"] for r in rows}

    def get(self, run_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute("SELECT * FROM builds WHERE run_id=?", (run_id,)).fetchone()

    def incomplete(self) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(
                """SELECT * FROM builds
                   WHERE status NOT IN ('completed','lost')
                      OR (callback_state='pending' AND callback_attempts < ?)""",
                (MAX_CALLBACK_ATTEMPTS,),
            ).fetchall()

    def update(self, run_id: int, **fields) -> None:
        fields["updated_at"] = utcnow()
        sets = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE builds SET {sets} WHERE run_id=?",
                   tuple(fields.values()) + (run_id,))


# --------------------------------------------------------------------------
# GitHub client
# --------------------------------------------------------------------------
class BridgeError(Exception):
    """GitHub-side failure; carries an HTTP status to surface to the caller."""

    def __init__(self, http_status: int, detail: str):
        super().__init__(detail)
        self.http_status = http_status
        self.detail = detail


class GitHub:
    def __init__(self):
        self._client = httpx.AsyncClient(
            base_url=GITHUB_API,
            timeout=30,
            headers={
                "Authorization": f"Bearer {GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "wheels-bridge/1.0",
            },
        )
        # Serializes dispatch+find-run so concurrent triggers never claim the
        # same freshly created run (single-instance deployment assumption;
        # see README for multi-instance notes).
        self.dispatch_lock = asyncio.Lock()

    async def aclose(self):
        await self._client.aclose()

    def _map_error(self, r: httpx.Response, action: str):
        if r.status_code in (401, 403):
            raise BridgeError(502, f"GitHub rejected the bridge token on {action} "
                                   f"({r.status_code}); check GITHUB_TOKEN scopes "
                                   "(needs Actions: Read and write)")
        if r.status_code == 404:
            raise BridgeError(400, f"{action}: not found (404) — wrong ref, workflow "
                                   f"filename, or the token cannot see the repo")
        if r.status_code == 422:
            raise BridgeError(400, f"{action}: inputs rejected by GitHub (422): "
                                   f"{r.text[:300]}")
        raise BridgeError(502, f"{action}: unexpected GitHub response "
                               f"{r.status_code}: {r.text[:300]}")

    async def dispatch(self, ref: str, inputs: dict[str, str]) -> None:
        r = await self._client.post(
            f"/repos/{GITHUB_REPO}/actions/workflows/{WORKFLOW_FILE}/dispatches",
            json={"ref": ref, "inputs": inputs},
        )
        if r.status_code != 204:
            self._map_error(r, "workflow dispatch")

    async def find_run(self, ref: str, since: str, exclude: set[int]) -> dict | None:
        """Locate the run created by our dispatch (the API returns 204 with no id).

        Matched by workflow path + head branch + creation time, minus any run
        this bridge has already registered.
        """
        r = await self._client.get(
            f"/repos/{GITHUB_REPO}/actions/runs",
            params={"event": "workflow_dispatch", "per_page": 15},
        )
        if r.status_code != 200:
            self._map_error(r, "runs list")
        best = None
        for run in r.json().get("workflow_runs", []):
            if run.get("path") != WORKFLOW_PATH or run.get("head_branch") != ref:
                continue
            if run["created_at"] < since or run["id"] in exclude:
                continue
            if best is None or run["created_at"] < best["created_at"]:
                best = run
        return best

    async def get_run(self, run_id: int) -> dict:
        r = await self._client.get(f"/repos/{GITHUB_REPO}/actions/runs/{run_id}")
        if r.status_code == 404:
            raise BridgeError(404, f"run {run_id} not found")
        if r.status_code != 200:
            self._map_error(r, f"run {run_id} lookup")
        return r.json()

    async def list_artifacts(self, run_id: int) -> list[dict]:
        r = await self._client.get(
            f"/repos/{GITHUB_REPO}/actions/runs/{run_id}/artifacts",
            params={"per_page": 100},
        )
        if r.status_code != 200:
            return []  # best effort; artifacts are a convenience next to OBS
        return [
            {
                "name": a["name"],
                "archive_download_url": a["archive_download_url"],
                "size_in_bytes": a.get("size_in_bytes"),
                "expires_at": a.get("expires_at"),
            }
            for a in r.json().get("artifacts", [])
            if a["name"].startswith("cibw-wheels-")
        ]


# --------------------------------------------------------------------------
# OBS nightly index (public, unauthenticated): best-effort wheel file match
# --------------------------------------------------------------------------
def _embedded_date(name: str) -> str:
    """Build date (YYYYMMDD) embedded in a wheel filename; '' sorts last."""
    m = re.search(r"(\d{8})", name)
    return m.group(1) if m else ""


async def obs_wheel_files(inputs: dict) -> tuple[str, list[str]]:
    """Return (index_url, matching .whl filenames) from the OBS nightly index.

    Matching is by cp tag and arch from the requested inputs, restricted to
    the 3 most recent build dates (newest first, max 100 files). The index
    accumulates across builds and is not run-scoped, so callers should prefer
    github_artifacts for run-exact downloads and use this for the public
    pip-installable location.
    """
    base = OBS_INDEX_URL.rsplit("/", 1)[0] + "/"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(OBS_INDEX_URL)
            r.raise_for_status()
    except Exception as exc:  # network / 404 — degrade gracefully
        log.warning("OBS index fetch failed: %s", exc)
        return base, []
    files = re.findall(r"<a href='([^']+\.whl)'", r.text) or \
            re.findall(r'<a href="([^"]+\.whl)"', r.text)
    pys = VALID_PYTHON if inputs.get("python") == "all" \
        else set(inputs.get("python", "all").split(","))
    archs = {"x86_64", "aarch64"} if inputs.get("arch") == "all" \
        else {inputs.get("arch", "all")}
    # Platform tags observed in the index: linux_x86_64 (plain) and
    # manylinux_2_27_x86_64.manylinux_2_28_x86_64 (cibuildwheel output) —
    # match any *linux*_<arch> token rather than only the plain form.
    matched = [
        f for f in files
        if any(f"-{py}-" in f for py in pys)
        and any(re.search(rf"linux[0-9_]*_{a}\b", f) for a in archs)
    ]
    # The index accumulates every historical build. Keep only the 3 most
    # recent build dates (filenames embed YYYYMMDD), newest first, capped —
    # otherwise the callback would carry hundreds of stale URLs.
    matched.sort(key=lambda f: (_embedded_date(f), f), reverse=True)
    recent_dates = list(dict.fromkeys(_embedded_date(f) for f in matched))[:3]
    if recent_dates:
        cutoff = recent_dates[-1]
        matched = [f for f in matched if _embedded_date(f) >= cutoff]
    return base, matched[:100]


# --------------------------------------------------------------------------
# Callback delivery (HMAC-signed, retried by the poller)
# --------------------------------------------------------------------------
def build_callback_payload(row: sqlite3.Row) -> dict:
    return {
        "event": "wheel_build.completed",
        "run_id": row["run_id"],
        "run_url": row["run_url"],
        "repo": row["repo"],
        "ref": row["ref"],
        "status": row["status"],
        "conclusion": row["conclusion"],
        "completed_at": row["completed_at"],
        "inputs": json.loads(row["inputs_json"]),
        "wheels": json.loads(row["wheels_json"]) if row["wheels_json"] else None,
    }


async def deliver_callback(client: httpx.AsyncClient, row: sqlite3.Row, store: Store) -> None:
    payload = build_callback_payload(row)
    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "wheels-bridge/1.0",
        "X-Wheels-Bridge-Event": payload["event"],
        "X-Wheels-Bridge-Run-Id": str(row["run_id"]),
    }
    secret = row["callback_secret"] or ""
    if secret:
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        headers["X-Wheels-Bridge-Signature"] = f"sha256={sig}"
    else:
        log.warning("callback for run %s is UNSIGNED (no callback secret configured)",
                    row["run_id"])
    try:
        r = await client.post(row["callback_url"], content=body, headers=headers, timeout=15)
        ok = 200 <= r.status_code < 300
    except Exception as exc:
        log.warning("callback for run %s failed: %s", row["run_id"], exc)
        ok = False
    attempts = row["callback_attempts"] + 1
    if ok:
        store.update(row["run_id"], callback_state="delivered",
                     callback_attempts=attempts)
        log.info("callback delivered for run %s", row["run_id"])
    else:
        state = "failed" if attempts >= MAX_CALLBACK_ATTEMPTS else "pending"
        store.update(row["run_id"], callback_state=state, callback_attempts=attempts)


# --------------------------------------------------------------------------
# Background poller
# --------------------------------------------------------------------------
async def poll_once(gh: GitHub, store: Store, client: httpx.AsyncClient) -> None:
    for row in store.incomplete():
        run_id = row["run_id"]
        try:
            if row["status"] not in ("completed", "lost"):
                try:
                    run = await gh.get_run(run_id)
                except BridgeError as exc:
                    if exc.http_status == 404 and row["created_at"] < utc_ago(3600):
                        store.update(run_id, status="lost", conclusion="lost")
                        log.warning("run %s vanished; marking lost", run_id)
                        continue
                    raise
                update: dict = {"status": run["status"]}
                if run["status"] == "completed":
                    update["conclusion"] = run["conclusion"]
                    update["completed_at"] = run.get("updated_at") or utcnow()
                    # Gather wheel addresses once, at completion.
                    inputs = json.loads(row["inputs_json"])
                    artifacts = await gh.list_artifacts(run_id)
                    obs_base, obs_files = await obs_wheel_files(inputs)
                    update["wheels_json"] = json.dumps({
                        "github_artifacts": artifacts,
                        "obs_index_url": obs_base,
                        "obs_files": [obs_base + f for f in obs_files],
                    })
                    if not row["callback_url"]:
                        update["callback_state"] = "none"
                store.update(run_id, **update)
                row = store.get(run_id)  # refresh for the callback phase below
            if (row["callback_url"] and row["callback_state"] == "pending"
                    and row["status"] == "completed"
                    and row["callback_attempts"] < MAX_CALLBACK_ATTEMPTS):
                await deliver_callback(client, row, store)
        except Exception:
            log.exception("poll error for run %s", run_id)


async def poller_loop(gh: GitHub, store: Store, client: httpx.AsyncClient) -> None:
    while True:
        await asyncio.sleep(POLL_INTERVAL)
        try:
            await poll_once(gh, store, client)
        except Exception:
            log.exception("poller iteration failed")


# --------------------------------------------------------------------------
# App / routes
# --------------------------------------------------------------------------
store = Store(DB_PATH)
gh = GitHub()


def check_api_key(key: str) -> None:
    if not API_KEYS:
        raise HTTPException(503, "bridge misconfigured: BRIDGE_API_KEYS is empty")
    if not any(hmac.compare_digest(key, k) for k in API_KEYS):
        raise HTTPException(401, "invalid or missing X-API-Key")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not GITHUB_TOKEN:
        log.warning("GITHUB_TOKEN is not set — trigger/status calls will fail until it is")
    client = httpx.AsyncClient()  # plain client for outbound callbacks
    task = asyncio.create_task(poller_loop(gh, store, client))
    yield
    task.cancel()
    await client.aclose()
    await gh.aclose()


app = FastAPI(title="Wheels Build Bridge", version="1.0", lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    return {"ok": True, "repo": GITHUB_REPO, "workflow": WORKFLOW_FILE}


@app.post("/api/v1/wheel-builds", status_code=202)
async def trigger(req: TriggerRequest, x_api_key: str = Header(default="")):
    check_api_key(x_api_key)
    ref = req.ref or DEFAULT_REF
    inputs = {
        "arch": req.arch,
        "python": req.python,
        "branch": req.branch,
        "publish": "true" if req.publish else "false",
    }
    try:
        async with gh.dispatch_lock:
            # 5s margin absorbs clock skew between bridge and GitHub.
            since = utc_ago(5)
            known = store.known_run_ids()
            await gh.dispatch(ref, inputs)
            run = None
            deadline = time.monotonic() + FIND_RUN_TIMEOUT
            while time.monotonic() < deadline and run is None:
                run = await gh.find_run(ref, since, known)
                if run is None:
                    await asyncio.sleep(3)
    except BridgeError as exc:
        raise HTTPException(exc.http_status, exc.detail) from exc
    if run is None:
        raise HTTPException(
            502,
            "dispatch was accepted (204) but no matching run became visible "
            f"within {FIND_RUN_TIMEOUT}s; check the Actions tab — the build may "
            "still have started",
        )
    store.insert(
        run_id=run["id"], ref=ref, inputs=inputs,
        callback_url=req.callback_url,
        callback_secret=req.callback_secret or CALLBACK_SECRET or None,
        status=run["status"], run_url=run["html_url"],
    )
    log.info("dispatched run %s (ref=%s inputs=%s callback=%s)",
             run["id"], ref, inputs, bool(req.callback_url))
    return {
        "run_id": run["id"],
        "run_url": run["html_url"],
        "status": run["status"],
        "inputs": inputs,
        "ref": ref,
        "callback": {"registered": bool(req.callback_url),
                     "signed": bool(req.callback_secret or CALLBACK_SECRET)},
    }


@app.get("/api/v1/wheel-builds/{run_id}")
async def status(run_id: int, x_api_key: str = Header(default="")):
    check_api_key(x_api_key)
    row = store.get(run_id)
    if row is None:
        raise HTTPException(404, f"run {run_id} was not triggered through this bridge")
    return {
        "run_id": row["run_id"],
        "run_url": row["run_url"],
        "repo": row["repo"],
        "ref": row["ref"],
        "status": row["status"],
        "conclusion": row["conclusion"],
        "inputs": json.loads(row["inputs_json"]),
        "wheels": json.loads(row["wheels_json"]) if row["wheels_json"] else None,
        "callback": {
            "url": row["callback_url"],
            "state": row["callback_state"],
            "attempts": row["callback_attempts"],
        },
        "created_at": row["created_at"],
        "completed_at": row["completed_at"],
    }
