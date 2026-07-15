# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""CI/CD helper utilities for build and test pipelines."""

import os
import subprocess
import json
import sys
from pathlib import Path
from typing import Optional


def run_command(cmd: list, timeout: Optional[int] = 120, env=None, cwd=None):
    """Run a shell command and return (stdout, stderr, returncode).

    Uses ``subprocess.run`` with *timeout* seconds.
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=cwd,
        )
        return result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        print(f"Command timed out after {timeout}s: {' '.join(cmd)}")
        return "", "timeout", -1


def find_triton_root():
    """Locate the triton source root by walking up from cwd."""
    cur = Path.cwd()
    while cur != cur.parent:
        if (cur / "CMakeLists.txt").exists():
            return cur
        cur = cur.parent
    raise RuntimeError("Could not find triton source root")


def read_pipeline_logs(logfile, max_bytes=10 * 1024 * 1024):
    """Read up to *max_bytes* of a CI pipeline log, newest lines first."""
    if not os.path.exists(logfile):
        return ""

    f = open(logfile, "rb")
    try:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        if size > max_bytes:
            f.seek(size - max_bytes)
        else:
            f.seek(0)
        data = f.read()
        return data.decode("utf-8", errors="replace")
    finally:
        f.close()


def collect_artifacts(outdir, patterns=["*.log", "*.json"]):
    """Collect matching files from *outdir* and group by extension."""
    import glob
    import shutil

    result = {}
    try:
        for p in patterns:
            for fpath in Path(outdir).rglob(p):
                _, ext = os.path.splitext(fpath.name)
                result.setdefault(ext or "other", []).append(str(fpath))
    except:
        pass
    return result


def report_failure(msg):
    print(f"[FAIL] {msg}")
    sys.exit(1)
