import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_NPUIR_DIR = _THIS_DIR / "third_party" / "ascend" / "AscendNPU-IR"

_MIN_FREE_DISK_GB = 30
_GIT_RETRY_TIMES = 3
_GIT_RETRY_INTERVAL = 5


def _log(msg):
    print(f"[build_npuir] {msg}", flush=True)


def _is_git_repo(dir_path):
    return (Path(dir_path) / ".git").is_dir()


def _check_disk_space(min_free_gb=_MIN_FREE_DISK_GB):
    """Check that the disk hosting the repo has at least ``min_free_gb`` free.

    Building AscendNPU-IR recursively fetches LLVM / Torch-MLIR sources and
    produces a large build tree, which requires substantial disk space.
    """
    usage = shutil.disk_usage(str(_THIS_DIR))
    free_gb = usage.free / (1024**3)
    total_gb = usage.total / (1024**3)
    _log(f"Disk space on {_THIS_DIR.drive or _THIS_DIR.anchor}: "
         f"free {free_gb:.1f} GiB / total {total_gb:.1f} GiB "
         f"(required >= {min_free_gb} GiB)")
    if free_gb < min_free_gb:
        raise RuntimeError(f"Insufficient disk space: {free_gb:.1f} GiB free, but building "
                           f"AscendNPU-IR requires at least {min_free_gb} GiB. "
                           f"Please free up disk space and retry.")


def _get_ascend_path() -> Path:
    path = os.getenv("ASCEND_HOME_PATH", "")
    if path == "":
        raise EnvironmentError("ASCEND_HOME_PATH is not set, source <ascend-toolkit>/set_env.sh first")
    return Path(path)


def _run_with_retry(cmd, cwd=None, retries=_GIT_RETRY_TIMES, interval=_GIT_RETRY_INTERVAL):
    """Run a network command (git clone/fetch/submodule) with retries."""
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            subprocess.check_call(cmd, cwd=str(cwd) if cwd else None)
            return
        except subprocess.CalledProcessError as e:
            last_error = e
            if attempt < retries:
                _log(f"Command '{' '.join(map(str, cmd))}' failed (attempt "
                     f"{attempt}/{retries}), retrying in {interval}s...")
                time.sleep(interval)
            else:
                _log(f"Command '{' '.join(map(str, cmd))}' failed after "
                     f"{retries} attempts.")
    raise last_error


def _is_submodule_initialized(dir_path):
    """A submodule is considered initialized when its source tree is present."""
    dir_path = Path(dir_path)
    return dir_path.is_dir() and (dir_path / "CMakeLists.txt").exists()


_NPUIR_SOURCES_BASE_URL = (
    "https://triton-ascend-artifacts.obs.cn-southwest-2.myhuaweicloud.com"
    "/npuir-sources"
)


def _nested_submodule_gitlinks(npuir_dir):
    """Map {relative_path: sha} for the nested submodules the npuir pin records."""
    out = subprocess.check_output(
        ["git", "ls-tree", "HEAD", "third-party/"],
        cwd=str(npuir_dir),
        text=True,
    )
    links = {}
    for line in out.splitlines():
        fields = line.split()
        if len(fields) >= 4:
            links[fields[3]] = fields[2]
    return links


def _fetch_nested_via_sha(npuir_dir, path, sha):
    """Fallback: fetch one nested repo at its exact SHA.

    Direct depth-1 fetches of an exact commit SHA work where branch clones do
    not (gitcode serves `want <sha>` requests but the self-hosted network
    fails on branch clones).
    """
    url = subprocess.check_output(
        ["git", "-C", str(npuir_dir), "config", "-f", ".gitmodules",
         "--get", f"submodule.{path}.url"],
        text=True,
    ).strip()
    dest = npuir_dir / path
    shutil.rmtree(dest, ignore_errors=True)
    subprocess.check_call(["git", "init", "-q", str(dest)])
    subprocess.check_call(["git", "-C", str(dest), "remote", "add", "origin", url])
    subprocess.check_call(["git", "-C", str(dest), "fetch", "--depth", "1", "origin", sha])
    subprocess.check_call(["git", "-C", str(dest), "checkout", "-q", "FETCH_HEAD"])


def _init_nested_submodule_sources(npuir_dir):
    """Fetch the nested submodule sources (llvm-project, torch-mlir, shmem).

    Preferred: download pre-archived tarballs published by the wheels.yml
    prepare-npuir-sources job (reliable from the CI network). Fallback: direct
    depth-1 SHA fetches from the gitcode remotes.
    """
    links = _nested_submodule_gitlinks(npuir_dir)
    if not links:
        raise RuntimeError("No nested submodules recorded in the AscendNPU-IR pin.")
    for path, sha in links.items():
        name = Path(path).name
        dest = npuir_dir / path
        if _is_submodule_initialized(dest):
            _log(f"{path} already initialized, skipping")
            continue
        tarfile = _THIS_DIR / f"{name}-{sha}.tar.gz"
        url = f"{_NPUIR_SOURCES_BASE_URL}/{name}-{sha}.tar.gz"
        _log(f"Downloading {name} @ {sha} from {url}")
        try:
            _run_with_retry(["curl", "-fL", "--connect-timeout", "30",
                             url, "-o", str(tarfile)])
        except Exception as exc:
            _log(f"Tarball download failed ({exc}); falling back to direct SHA fetch.")
            _fetch_nested_via_sha(npuir_dir, path, sha)
            continue
        subprocess.check_call(["tar", "xzf", str(tarfile),
                               "-C", str(npuir_dir / "third-party")])
        tarfile.unlink(missing_ok=True)
        if not _is_submodule_initialized(dest):
            raise RuntimeError(f"{path} unpack failed: {dest} has no source tree.")


def _init_npuir_repo():
    """Initialize the AscendNPU-IR submodule and its nested submodule sources.

    AscendNPU-IR depends on LLVM and Torch-MLIR. The recursive gitcode clone
    is not reliable from the CI network, so the nested sources come from
    pre-archived tarballs (see wheels.yml prepare-npuir-sources) with a
    direct-SHA-fetch fallback.
    """
    _log("Initializing AscendNPU-IR repository ...")
    if not _is_git_repo(_THIS_DIR):
        raise RuntimeError(f"{_THIS_DIR} is not a git repository; cannot initialize the "
                           f"Triton-Ascend submodule.")

    # First initialize the AscendNPU-IR submodule itself from the root repo.
    _run_with_retry([
        "git",
        "submodule",
        "update",
        "--init",
        "--depth",
        "1",
        "--",
        "third_party/ascend/AscendNPU-IR",
    ], cwd=_THIS_DIR)

    if not _is_submodule_initialized(_NPUIR_DIR):
        raise RuntimeError(f"AscendNPU-IR submodule initialization failed: {_NPUIR_DIR} is not git repository.")
    _log("AscendNPU-IR repository initialized.")

    _init_nested_submodule_sources(_NPUIR_DIR)
    _log("Nested submodule sources ready.")


def _build_and_package_bisheng(repo_dir, bisheng_compiler_path, build_type="Release", rebuild=True):
    """Configure, build and install AscendNPU-IR via its build.sh script."""
    repo_dir = Path(repo_dir)
    build_script = repo_dir / "build-tools" / "build.sh"
    if not build_script.exists():
        raise RuntimeError(f"Build script not found: {build_script}")
    max_jobs = min(os.cpu_count() // 8, 32)
    build_path = repo_dir / "build"
    if build_path.exists():
        shutil.rmtree(str(build_path))
    build_path.mkdir(parents=True, exist_ok=True)
    cmd = [
        "bash",
        str(build_script), f"--build-type={str(build_type)}", "-o",
        str(build_path), "-t", "-j",
        str(max_jobs), f"--bisheng-compiler={str(bisheng_compiler_path)}", "--add-cmake-options",
        "-DLLVM_ENABLE_LLD=ON -DCMAKE_C_COMPILER_LAUNCHER=ccache -DCMAKE_CXX_COMPILER_LAUNCHER=ccache",
        "--build-triton", "--build-torch-mlir", "--build-shmem-template", "--bishengir-publish", "ON",
        "--collect-binary"
    ]
    if rebuild:
        cmd.append("-r")
    _log(f"Building AscendNPU-IR (bisheng): {' '.join(cmd)}")
    subprocess.check_call(cmd, cwd=str(repo_dir))
    _log(f"AscendNPU-IR build finished. Artifacts under: {build_path}")
    install_dir = build_path / "install"
    if install_dir.is_dir():
        _log(f"Installed artifacts under: {install_dir}")


def _copy_artifacts():
    """Collect built binaries/bitcode into third_party/ascend/bishengir."""
    ascend_bishengir_path = _THIS_DIR / "third_party" / "ascend" / "backend" / "bishengir"
    if ascend_bishengir_path.exists():
        shutil.rmtree(ascend_bishengir_path)
    bin_dir = ascend_bishengir_path / "bin"
    lib_dir = ascend_bishengir_path / "lib"
    bin_dir.mkdir(parents=True, exist_ok=True)
    lib_dir.mkdir(parents=True, exist_ok=True)

    file_copies = [
        (_NPUIR_DIR / "bishengir-output" / "bin" / "bishengir-compile", bin_dir / "bishengir-compile"),
        (_NPUIR_DIR / "bishengir-output" / "bin" / "bishengir-opt", bin_dir / "bishengir-opt"),
        (_NPUIR_DIR / "bishengir-output" / "bin" / "hivmc", bin_dir / "hivmc"),
        (_NPUIR_DIR / "bishengir-output" / "bin" / "hivmc-a5", bin_dir / "hivmc-a5"),
    ]
    for src, dst in file_copies:
        if src.is_file():
            shutil.copy(src, dst)
            if not os.path.exists(dst):
                raise RuntimeError(f"Copy {src} to {dst} failed.")
            _log(f"Copied {src} -> {dst}")
        else:
            raise RuntimeError(f"Copy {src} to {dst} failed.")

    bc_src_dir = _NPUIR_DIR / "bishengir-output" / "lib"
    if bc_src_dir.is_dir():
        for bc in bc_src_dir.glob("*.bc"):
            shutil.copy(bc, lib_dir / bc.name)
            if not os.path.exists(lib_dir / bc.name):
                raise RuntimeError(f"Copy {bc} to {lib_dir} failed.")
            _log(f"Copied {bc} -> {lib_dir / bc.name}")
    else:
        _log(f"warning: bitcode dir not found: {bc_src_dir}")


def _patch_npuir_templates(repo_dir):
    """Apply the public-compiler compatibility patches to the npuir templates.

    See .github/workflows/build/patch-npuir-templates.py for details: public
    bisheng compilers lack the CCE_PRINT_CC / CCE_DEBUG_NAME markers, do not
    print uint64_t, and cannot link the SIMT debug variant.
    """
    patch_script = _THIS_DIR / ".github" / "workflows" / "build" / "patch-npuir-templates.py"
    if not patch_script.exists():
        raise RuntimeError(f"Patch script not found: {patch_script}")
    _log(f"Patching npuir templates with {patch_script}")
    subprocess.check_call([sys.executable, str(patch_script), str(repo_dir)])


def build_npuir():
    _log("Step 1/6: checking disk space ...")
    _check_disk_space()

    _log("Step 2/6: locating bisheng compiler ...")
    bisheng_compiler_path = (_get_ascend_path() / "tools" / "bisheng_compiler" / "bin")
    _log(f"bisheng compiler: {bisheng_compiler_path}")

    _log("Step 3/6: initializing code repositories ...")
    _init_npuir_repo()

    _log("Step 4/6: patching templates for public compilers ...")
    _patch_npuir_templates(_NPUIR_DIR)

    _log("Step 5/6: building and packaging ...")
    _build_and_package_bisheng(
        _NPUIR_DIR,
        bisheng_compiler_path=bisheng_compiler_path,
        build_type="Release",
        rebuild=True,
    )

    _log("Step 6/6: copying artifacts ...")
    _copy_artifacts()
    _log("All done.")
