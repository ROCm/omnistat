# -------------------------------------------------------------------------------
# MIT License
#
# Copyright (c) 2023 - 2026 Advanced Micro Devices, Inc. All Rights Reserved.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
# -------------------------------------------------------------------------------

"""Post-install builder for Omnistat's optional ROCm extensions.

Compiles the optional native components against the ROCm currently active on the
host, so a user who installed Omnistat from a wheel can enable them without a
source checkout. The C++ sources ship inside the package (omnistat/rocprofiler-sdk).

Two capabilities, three artifacts (all from the same source tree):

  --counters  builds the rocprofiler_sdk_extension Python module (installed into
              the omnistat package so it is importable) and libomnistat_count.so
  --tracing   builds libomnistat_trace.so

The two tool libraries (libomnistat_count.so, libomnistat_trace.so) are not
imported; they are loaded into a GPU application via ROCP_TOOL_LIBRARIES. Print
the resolved path for that variable with --print-trace-lib / --print-count-lib.

ROCm is discovered from the active environment, so after switching ROCm modules
(e.g. `module swap rocm rocm/2`) re-run this command to rebuild.
"""

import argparse
import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from importlib import resources
from pathlib import Path

TRACE_LIB = "libomnistat_trace.so"
COUNT_LIB = "libomnistat_count.so"
BUILD_INFO = "build_info.json"

_SAFE_CWD = None


def _safe_cwd():
    """A neutral working directory for subprocesses.

    Every child we spawn (pip, and cmake -- which itself runs `python -m nanobind`)
    inherits our cwd, and `python -m` prepends the cwd to sys.path. Running them
    from an empty directory prevents a stdlib-shadowing file in the caller's cwd
    (e.g. a local enum.py/re.py) from breaking those subprocesses.
    """
    global _SAFE_CWD
    if _SAFE_CWD is None:
        _SAFE_CWD = tempfile.mkdtemp(prefix="omnistat-ext-cwd-")
        atexit.register(shutil.rmtree, _SAFE_CWD, ignore_errors=True)
    return _SAFE_CWD


def _package_dir():
    import omnistat

    return Path(omnistat.__file__).resolve().parent


def _sources_dir():
    """Filesystem path to the shipped C++ sources (omnistat/rocprofiler-sdk)."""
    src = resources.files("omnistat").joinpath("rocprofiler-sdk")
    path = Path(str(src))
    if not (path / "CMakeLists.txt").exists():
        sys.exit(
            f"ERROR: extension sources not found at {path}. This Omnistat install "
            "does not include the extension build sources."
        )
    return path


def _default_lib_dir():
    return _package_dir() / "rocprofiler-sdk" / "lib"


def _detect_rocm(explicit):
    """Return (rocm_path, rocm_version) for the active ROCm, or (None, None)."""
    rocm_path = explicit or os.environ.get("ROCM_PATH")
    if not rocm_path:
        hipconfig = shutil.which("hipconfig")
        if hipconfig:
            rocm_path = str(Path(hipconfig).resolve().parent.parent)
    if not rocm_path or not Path(rocm_path).exists():
        return None, None

    version = None
    version_file = Path(rocm_path) / ".info" / "version"
    if version_file.exists():
        version = version_file.read_text().strip()
    else:
        hipconfig = shutil.which("hipconfig")
        if hipconfig:
            try:
                version = subprocess.check_output([hipconfig, "--version"], text=True).strip()
            except subprocess.CalledProcessError:
                version = None
    return rocm_path, version


def _ensure_build_deps():
    """Install cmake/nanobind into the active environment if missing."""
    needed = []
    if shutil.which("cmake") is None:
        needed.append("cmake")
    try:
        import nanobind  # noqa: F401
    except ImportError:
        needed.append("nanobind<3.0")
    if needed:
        print(f"==> Installing build dependencies: {' '.join(needed)}")
        subprocess.check_call([sys.executable, "-m", "pip", "install", *needed], cwd=_safe_cwd())


def _cmake_prefix_args(rocm_path):
    return [f"-DCMAKE_PREFIX_PATH={rocm_path}"] if rocm_path else []


def _run(cmd):
    print(f"==> {' '.join(str(c) for c in cmd)}")
    subprocess.check_call([str(c) for c in cmd], cwd=_safe_cwd())


def _build_counter_module(src, rocm_path, jobs):
    """Configure/build/install the nanobind module into the omnistat package."""
    with tempfile.TemporaryDirectory(prefix="omnistat-ext-module-") as build_dir:
        _run(
            [
                "cmake",
                "-S",
                src,
                "-B",
                build_dir,
                f"-DPython_EXECUTABLE={sys.executable}",
                *_cmake_prefix_args(rocm_path),
            ]
        )
        _run(["cmake", "--build", build_dir, "-j", str(jobs)])
        # install(TARGETS ... DESTINATION omnistat) -> <prefix>/omnistat/<module>.so
        _run(["cmake", "--install", build_dir, "--prefix", _package_dir().parent])
    print(f"    installed rocprofiler_sdk_extension into {_package_dir()}")


def _build_tool_libs(src, rocm_path, jobs, build_trace, build_count, build_dir, output_dir, name, force):
    """Build the requested tool libraries and place them in the output directory."""
    persistent = build_dir is not None
    work = Path(build_dir) if persistent else Path(tempfile.mkdtemp(prefix="omnistat-ext-libs-"))
    if persistent and work.exists() and force:
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    # Drop any cached configuration so a fresh configure re-detects the active
    # ROCm (avoids a stale CMakeCache.txt pinning a previously loaded module).
    (work / "CMakeCache.txt").unlink(missing_ok=True)
    try:
        cmake_args = ["cmake", "-S", src, "-B", str(work), *_cmake_prefix_args(rocm_path)]
        if build_trace:
            cmake_args.append("-DBUILD_KERNEL_TRACE_LIB=ON")
        if build_count:
            cmake_args.append("-DBUILD_COUNT_LIB=ON")
        _run(cmake_args)
        _run(["cmake", "--build", str(work), "-j", str(jobs)])

        produced = []
        if build_trace:
            produced.append(TRACE_LIB)
        if build_count:
            produced.append(COUNT_LIB)
        if name and len(produced) != 1:
            sys.exit("ERROR: --name may only be used when building a single tool library")

        dest_dir = Path(output_dir) if output_dir else _default_lib_dir()
        dest_dir.mkdir(parents=True, exist_ok=True)
        placed = {}
        for lib in produced:
            built = work / lib
            if not built.exists():
                sys.exit(f"ERROR: expected build output {built} not found")
            dest = dest_dir / (name if name else lib)
            shutil.copy2(built, dest)
            placed[lib] = dest
            print(f"    built {lib} -> {dest}")
        return placed
    finally:
        if not persistent:
            shutil.rmtree(work, ignore_errors=True)


def _build_workloads():
    """Best-effort (re)build of the GPU test workloads shipped in omnistat_tests.

    The counter and kernel-tracing tests launch real GPU workloads, so building
    the extensions is not enough to exercise them. hipcc auto-selects the locally
    detected GPU arch, so this rebuilds for whatever node/queue it runs on. It is
    skipped when omnistat_tests is not installed, and a build failure is reported
    but never aborts the extension build.
    """
    try:
        import omnistat_tests.workloads as workloads
    except ImportError:
        print("==> omnistat_tests not installed; skipping test workload build")
        return

    workloads_dir = Path(workloads.__file__).resolve().parent
    if not (workloads_dir / "Makefile").exists():
        print(f"==> no workload Makefile under {workloads_dir}; skipping")
        return

    print(f"==> Building test workloads in {workloads_dir}")
    try:
        _run(["make", "-C", str(workloads_dir), "clean"])
        _run(["make", "-C", str(workloads_dir)])
    except subprocess.CalledProcessError:
        print("WARNING: test workload build failed", file=sys.stderr)


def _write_build_info(rocm_path, rocm_version):
    info = {
        "rocm_path": rocm_path,
        "rocm_version": rocm_version,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    lib_dir = _default_lib_dir()
    lib_dir.mkdir(parents=True, exist_ok=True)
    (lib_dir / BUILD_INFO).write_text(json.dumps(info, indent=2))


def _print_lib(lib_name, explicit_rocm):
    """Print only the resolved default path for a tool library (for ROCP_TOOL_LIBRARIES)."""
    lib = _default_lib_dir() / lib_name
    if not lib.exists():
        sys.exit(
            f"ERROR: {lib_name} not found at {lib}. Build it first with "
            "`omnistat-build-extras` (or pass the --build-dir/--output-dir you used)."
        )
    info_file = _default_lib_dir() / BUILD_INFO
    if info_file.exists():
        _, active_version = _detect_rocm(explicit_rocm)
        try:
            built_version = json.loads(info_file.read_text()).get("rocm_version")
        except (json.JSONDecodeError, OSError):
            built_version = None
        if built_version and active_version and built_version != active_version:
            print(
                f"WARNING: {lib_name} was built against ROCm {built_version} but the active "
                f"ROCm is {active_version}; rebuild with `omnistat-build-extras`.",
                file=sys.stderr,
            )
    print(lib)


def main():
    parser = argparse.ArgumentParser(
        prog="omnistat-build-extras",
        description="Build Omnistat's optional ROCm extensions against the active ROCm.",
    )
    parser.add_argument("--counters", action="store_true", help="Build the hardware-counter components")
    parser.add_argument("--tracing", action="store_true", help="Build the kernel-tracing library")
    parser.add_argument("--build-dir", help="cmake build directory for the tool libraries (persisted if set)")
    parser.add_argument("--output-dir", help="Directory to place built tool libraries (default: inside the package)")
    parser.add_argument("--name", help="Override the output filename for a single tool library")
    parser.add_argument("--jobs", "-j", type=int, default=os.cpu_count() or 1, help="Parallel build jobs")
    parser.add_argument("--rocm-path", help="Path to the ROCm installation (default: $ROCM_PATH or hipconfig)")
    parser.add_argument("--force", action="store_true", help="Force a clean rebuild")
    parser.add_argument(
        "--no-workloads",
        dest="workloads",
        action="store_false",
        help="Skip (re)building the GPU test workloads from omnistat_tests",
    )
    parser.add_argument("--print-trace-lib", action="store_true", help="Print the trace library path and exit")
    parser.add_argument("--print-count-lib", action="store_true", help="Print the count library path and exit")
    args = parser.parse_args()

    # Resolve user paths against the real cwd now; subprocesses run elsewhere.
    if args.build_dir:
        args.build_dir = str(Path(args.build_dir).resolve())
    if args.output_dir:
        args.output_dir = str(Path(args.output_dir).resolve())

    if args.print_trace_lib:
        _print_lib(TRACE_LIB, args.rocm_path)
        return
    if args.print_count_lib:
        _print_lib(COUNT_LIB, args.rocm_path)
        return

    # Default to building everything when no capability is selected.
    build_counters = args.counters or not (args.counters or args.tracing)
    build_tracing = args.tracing or not (args.counters or args.tracing)

    src = _sources_dir()
    rocm_path, rocm_version = _detect_rocm(args.rocm_path)
    if rocm_path is None:
        sys.exit("ERROR: could not locate a ROCm installation (set --rocm-path or $ROCM_PATH).")
    print(f"==> Using ROCm at {rocm_path}" + (f" (version {rocm_version})" if rocm_version else ""))

    _ensure_build_deps()

    if build_counters:
        _build_counter_module(src, rocm_path, args.jobs)

    if build_tracing or build_counters:
        placed = _build_tool_libs(
            src,
            rocm_path,
            args.jobs,
            build_trace=build_tracing,
            build_count=build_counters,
            build_dir=args.build_dir,
            output_dir=args.output_dir,
            name=args.name,
            force=args.force,
        )
        _write_build_info(rocm_path, rocm_version)

        if build_tracing:
            hint = placed.get(TRACE_LIB, _default_lib_dir() / TRACE_LIB)
            print(f"\nTo enable kernel tracing, set:\n  export ROCP_TOOL_LIBRARIES={hint}")

    if args.workloads:
        _build_workloads()

    print("\nDone.")


if __name__ == "__main__":
    main()
