"""
plugins/llama-cpp/backends/sycl_windows.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
llama.cpp backend for Intel Arc GPUs on Windows via Intel oneAPI (SYCL).

Tested hardware: Intel Arc A750 (8GB VRAM), Windows 10/11.
Covers all Arc discrete GPUs and Intel integrated GPUs with level_zero support.

cmake command used (run inside setvars.bat environment):
    cmake -B build -G "Ninja"
          -DGGML_SYCL=ON
          -DCMAKE_C_COMPILER=cl
          -DCMAKE_CXX_COMPILER=icx
          -DCMAKE_BUILD_TYPE=Release
          [-DGGML_SYCL_F16=ON]       (from build config, default ON)

Key differences from sycl_linux.py:
    - No `source` — setvars.bat is activated by capturing its env output
      via a cmd /c subprocess and merging it into the build environment.
    - C compiler is MSVC `cl`, CXX compiler is Intel `icx` (not icx-cl).
    - Generator is Ninja (not default MSVC generator) — required for
      the cl+icx mixed-compiler setup to work correctly.
    - Binary path is build\\bin\\llama-server.exe
    - No render/video group membership checks — not a Windows concept.
    - Remedies use winget where automatable; manual instructions otherwise.
    - winget itself is checked first and its absence degrades gracefully
      to manual instructions for all winget-dependent remedies.

Known Windows-specific gotchas documented here so they surface in remedy
messages rather than leaving the user confused:
    - sycl-ls must be run inside the setvars.bat environment or it finds
      nothing, even if oneAPI is installed.
    - The Microsoft OpenCL/Vulkan Compatibility Pack (installed silently
      alongside Arc drivers on some machines) can block sycl device
      detection. Remedy message tells user to check Microsoft Store.
    - GPU driver version matters: minimum 31.0.101.5522 for stable
      level_zero operation. We check the driver version via wmic.
"""

from __future__ import annotations

import os
import re
import subprocess
import traceback
from pathlib import Path
from typing import Any

from aurini.core.base import CheckResult, RemedyResult
from aurini.core.checks import (
    command_exists,
    disk_space_gte,
    directory_writable,
    gpu_vendor_is,
    host_reachable,
    internet_reachable,
)
from aurini.core.log import RevertType
from plugins.llama_cpp.backends.base import LlamaCppBackend
from plugins.llama_cpp.backends import shared


# ── Constants ──────────────────────────────────────────────────────────────────

ONEAPI_SETVARS = Path("C:/Program Files (x86)/Intel/oneAPI/setvars.bat")
BINARY_RELATIVE = r"build\bin\llama-server.exe"

# Minimum Arc driver version known to work reliably with level_zero.
# Format matches wmic output: major.minor.build.revision as a tuple.
MIN_DRIVER_VERSION = (31, 0, 101, 5522)

# cmake flags that are the same regardless of build config.
# Note: -DCMAKE_C_COMPILER=cl uses MSVC for C (required by Ninja+icx on Win),
#       -DCMAKE_CXX_COMPILER=icx uses the Intel compiler for C++ (SYCL).
#       These two working together is the correct Windows SYCL build setup.
CMAKE_FLAGS_BASE = [
    '-G "Ninja"',
    "-DGGML_SYCL=ON",
    "-DGGML_SYCL_TARGET=INTEL",
    "-DGGML_SYCL_DNN=ON",
    "-DCMAKE_C_COMPILER=cl",
    "-DCMAKE_CXX_COMPILER=icx",
    "-DCMAKE_BUILD_TYPE=Release",
]

# Winget package IDs for tools we can auto-install.
WINGET_PACKAGES = {
    "git":   "Git.Git",
    "cmake": "Kitware.CMake",
    "ninja": "Ninja-build.Ninja",
}


# ── Environment capture ────────────────────────────────────────────────────────

def _capture_setvars_env() -> dict[str, str] | None:
    """
    Run setvars.bat and capture the resulting environment variables.

    Writes a temporary batch file that calls setvars.bat then prints
    the environment via `set`. This is more reliable than passing a
    multi-line script to cmd /c directly, as env changes from setvars.bat
    are guaranteed to be visible to the subsequent set command.

    Returns the environment dict on success, or None on failure.
    """
    if not ONEAPI_SETVARS.exists():
        return None

    import tempfile

    # Write a temp .bat file — more reliable than a multi-line cmd /c string
    bat_content = (
        f'@echo off\r\n'
        f'call "{ONEAPI_SETVARS}" intel64 --force\r\n'
        f'set\r\n'
    )

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".bat",
            delete=False,
            encoding="utf-8",
        ) as f:
            f.write(bat_content)
            bat_path = f.name

        result = subprocess.run(
            ["cmd.exe", "/c", bat_path],
            capture_output=True,
            text=True,
            timeout=60,
            encoding="utf-8",
            errors="replace",
        )

        # Clean up temp file
        try:
            Path(bat_path).unlink()
        except Exception:
            pass

        if result.returncode != 0:
            return None

        env: dict[str, str] = {}
        for line in result.stdout.splitlines():
            line = line.strip()
            # Skip empty lines and setvars banner lines (start with :: or :)
            if not line or line.startswith(":"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # Skip empty or obviously invalid keys
            if not key or " " in key:
                continue
            env[key] = value

        return env if env else None

    except Exception:
        return None


def _run_in_setvars_env(
    command: list[str],
    timeout: int = 30,
) -> tuple[int, str]:
    """
    Run a command inside the setvars.bat environment.

    Used for checks that require the oneAPI environment to be active
    (e.g. sycl-ls). Returns (returncode, combined stdout+stderr).
    Returns (-1, error_message) if setvars.bat is missing or env capture fails.
    """
    env = _capture_setvars_env()
    if env is None:
        return -1, (
            "Could not activate Intel oneAPI environment. "
            "Check that oneAPI is installed at the expected path."
        )

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            encoding="utf-8",
            errors="replace",
        )
        combined = (result.stdout or "") + (result.stderr or "")
        return result.returncode, combined.strip()
    except FileNotFoundError:
        return -1, f"Command not found: {command[0]}"
    except Exception:
        return -1, traceback.format_exc()


# ── Driver version helpers ─────────────────────────────────────────────────────

def _get_arc_driver_version() -> tuple[int, ...] | None:
    """
    Return the Intel Arc GPU driver version as a tuple of ints, or None.

    Uses wmic to query the driver version from Win32_VideoController.
    Filters to the Intel GPU entry. Returns None if detection fails or
    no Intel GPU is found.
    """
    try:
        result = subprocess.run(
            [
                "wmic", "path", "win32_VideoController",
                "get", "Name,DriverVersion",
                "/format:csv",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            encoding="utf-8",
            errors="replace",
        )
        for line in result.stdout.splitlines():
            lower = line.lower()
            if "intel" in lower and ("arc" in lower or "iris" in lower or "uhd" in lower):
                # CSV format: Node,DriverVersion,Name
                parts = line.split(",")
                for part in parts:
                    part = part.strip()
                    # Driver versions look like 31.0.101.5522
                    if re.match(r"^\d+\.\d+\.\d+\.\d+$", part):
                        return tuple(int(x) for x in part.split("."))
        return None
    except Exception:
        return None


def _driver_version_ok(version: tuple[int, ...]) -> bool:
    """Return True if version meets or exceeds MIN_DRIVER_VERSION."""
    return version >= MIN_DRIVER_VERSION


def _fmt_version(v: tuple[int, ...]) -> str:
    return ".".join(str(x) for x in v)


# ── Backend ────────────────────────────────────────────────────────────────────

class SyclWindowsBackend(LlamaCppBackend):
    """
    Intel Arc GPU backend for llama.cpp on Windows via SYCL.

    install_path and job_log are injected by plugin.py before any
    system-modifying method is called.
    """

    def __init__(
        self,
        install_path: Path | None = None,
        job_log=None,
    ) -> None:
        self._install_path = install_path
        self._job_log      = job_log

        # Cached winget availability — checked once, reused across remedies.
        self._winget_available: bool | None = None

    # ── Identity ───────────────────────────────────────────────────────────────

    @property
    def backend_id(self) -> str:
        return "sycl_windows"

    @property
    def display_name(self) -> str:
        return "Intel Arc (SYCL) — Windows"

    # ── Pre-flight checks ──────────────────────────────────────────────────────

    def get_checks(self) -> list[str]:
        """
        Check order matters:

        1. winget_present      — determines whether auto-installs are possible.
                                 Checked first so later remedy descriptions are
                                 accurate (winget vs manual).
        2. vs2022_present      — required before cmake can find cl.exe. Manual only.
        3. oneapi_present      — toolkit must exist before gpu_visible can work.
        4. arc_driver_present  — driver version must be sufficient before sycl-ls works.
        5. git_present         — needed for clone/update.
        6. cmake_present       — needed for configure step.
        7. ninja_present       — required by the Ninja cmake generator.
        8. disk_space          — catch insufficient space before anything touches disk.
        9. internet_reachable  — needed for clone/update.
        10. github_reachable   — specifically needed for git operations.
        11. gpu_visible        — sycl-ls check; needs oneAPI env and correct driver.
        12. build_dir_writable — last, path may not be set yet at check time.
        """
        return [
            "winget_present",
            "vs2022_present",
            "oneapi_present",
            "arc_driver_present",
            "git_present",
            "cmake_present",
            "ninja_present",
            "disk_space",
            "internet_reachable",
            "github_reachable",
            "gpu_visible",
            "build_dir_writable",
        ]

    def run_check(self, check_id: str) -> CheckResult:
        try:
            return self._dispatch_check(check_id)
        except Exception:
            return CheckResult(
                check_id=check_id,
                passed=False,
                message=f"Unexpected error running check '{check_id}'. See raw output.",
                raw_output=traceback.format_exc(),
            )

    def _dispatch_check(self, check_id: str) -> CheckResult:

        # ── winget ────────────────────────────────────────────────────────────

        if check_id == "winget_present":
            # winget is included in Windows 10 1709+ via App Installer.
            # We treat its absence as informational — later remedies will
            # degrade to manual instructions automatically.
            import shutil as _shutil
            found = _shutil.which("winget") is not None
            self._winget_available = found
            if found:
                return CheckResult(
                    check_id=check_id,
                    passed=True,
                    message=(
                        "Windows Package Manager (winget) is available. "
                        "AURINI can automatically install some dependencies."
                    ),
                    raw_output="",
                )
            else:
                return CheckResult(
                    check_id=check_id,
                    passed=False,
                    message=(
                        "Windows Package Manager (winget) was not found. "
                        "AURINI will guide you through manual installation "
                        "of any missing dependencies instead."
                    ),
                    raw_output="",
                    remedy_id="remedy_install_winget",
                    risk="manual",
                )

        # ── Visual Studio 2022 ────────────────────────────────────────────────

        if check_id == "vs2022_present":
            # We detect VS2022 by looking for cl.exe via vswhere, which ships
            # with Visual Studio 2017+. If vswhere is absent we fall back to
            # checking the default VS2022 install path.
            found, raw = self._detect_vs2022()
            if found:
                return CheckResult(
                    check_id=check_id,
                    passed=True,
                    message="Visual Studio 2022 with C++ workload detected.",
                    raw_output=raw,
                )
            return CheckResult(
                check_id=check_id,
                passed=False,
                message=(
                    "Visual Studio 2022 with C++ workload was not found. "
                    "It is required to build llama.cpp on Windows — "
                    "the MSVC compiler (cl.exe) is used for the C build step."
                ),
                raw_output=raw,
                remedy_id="remedy_install_vs2022",
                risk="manual",
            )

        # ── oneAPI ────────────────────────────────────────────────────────────

        if check_id == "oneapi_present":
            if ONEAPI_SETVARS.exists():
                return CheckResult(
                    check_id=check_id,
                    passed=True,
                    message=f"Intel oneAPI found at: {ONEAPI_SETVARS}",
                    raw_output="",
                )
            return CheckResult(
                check_id=check_id,
                passed=False,
                message=(
                    "Intel oneAPI was not found at the expected path. "
                    f"Expected: {ONEAPI_SETVARS}\n"
                    "Intel oneAPI is required to build llama.cpp with "
                    "Intel Arc GPU support via SYCL."
                ),
                raw_output="",
                remedy_id="remedy_install_oneapi",
                risk="manual",
            )

        # ── Arc driver version ────────────────────────────────────────────────

        if check_id == "arc_driver_present":
            version = _get_arc_driver_version()
            if version is None:
                return CheckResult(
                    check_id=check_id,
                    passed=False,
                    message=(
                        "Could not detect Intel Arc GPU driver version. "
                        "Either no Intel GPU was found, or driver detection failed."
                    ),
                    raw_output="",
                    remedy_id="remedy_install_arc_driver",
                    risk="manual",
                )
            if _driver_version_ok(version):
                return CheckResult(
                    check_id=check_id,
                    passed=True,
                    message=(
                        f"Intel Arc GPU driver version {_fmt_version(version)} detected "
                        f"(minimum required: {_fmt_version(MIN_DRIVER_VERSION)})."
                    ),
                    raw_output="",
                )
            return CheckResult(
                check_id=check_id,
                passed=False,
                message=(
                    f"Intel Arc GPU driver version {_fmt_version(version)} is too old. "
                    f"Minimum required: {_fmt_version(MIN_DRIVER_VERSION)}. "
                    "Older drivers have known issues with level_zero GPU detection."
                ),
                raw_output="",
                remedy_id="remedy_install_arc_driver",
                risk="manual",
            )

        # ── git ───────────────────────────────────────────────────────────────

        if check_id == "git_present":
            return command_exists(
                check_id=check_id,
                command="git",
                remedy_id="remedy_install_git",
                risk="low",
            )

        # ── cmake ─────────────────────────────────────────────────────────────

        if check_id == "cmake_present":
            # cmake installed via Visual Studio ends up in a VS-internal path
            # that is not on the system PATH. Search known locations before
            # falling back to the winget remedy.
            cmake_path = self._find_cmake_windows()
            if cmake_path:
                return CheckResult(
                    check_id=check_id,
                    passed=True,
                    message=f"cmake found at: {cmake_path}",
                    raw_output="",
                )
            return command_exists(
                check_id=check_id,
                command="cmake",
                remedy_id="remedy_install_cmake",
                risk="low",
            )

        # ── ninja ─────────────────────────────────────────────────────────────

        if check_id == "ninja_present":
            return command_exists(
                check_id=check_id,
                command="ninja",
                remedy_id="remedy_install_ninja",
                risk="low",
            )

        # ── disk space ────────────────────────────────────────────────────────

        if check_id == "disk_space":
            path = self._install_path or Path.home()
            return disk_space_gte(
                check_id=check_id,
                path=path,
                minimum_gb=5.0,
            )

        # ── network ───────────────────────────────────────────────────────────

        if check_id == "internet_reachable":
            return internet_reachable(check_id=check_id)

        if check_id == "github_reachable":
            return host_reachable(
                check_id=check_id,
                host="github.com",
                port=443,
            )

        # ── GPU visible via sycl-ls ───────────────────────────────────────────

        if check_id == "gpu_visible":
            # sycl-ls must run inside the setvars.bat environment.
            # Running it bare will either not find it or return no devices.
            code, raw = _run_in_setvars_env(["sycl-ls"], timeout=30)

            if code != 0:
                return CheckResult(
                    check_id=check_id,
                    passed=False,
                    message=(
                        "sycl-ls failed to run inside the oneAPI environment. "
                        "This usually means oneAPI is not correctly installed, "
                        "or setvars.bat could not be activated."
                    ),
                    raw_output=raw,
                    remedy_id="remedy_gpu_not_visible",
                    risk="manual",
                )

            if "level_zero:gpu" in raw.lower():
                return CheckResult(
                    check_id=check_id,
                    passed=True,
                    message=(
                        "Intel Arc GPU detected via level_zero — "
                        "ready to build with SYCL."
                    ),
                    raw_output=raw,
                )

            # GPU not visible — build a helpful message based on what sycl-ls
            # did return, so the user has context for the most likely causes.
            return CheckResult(
                check_id=check_id,
                passed=False,
                message=(
                    "sycl-ls ran but no level_zero GPU was detected. "
                    "You can continue, but llama.cpp may not use your GPU at runtime."
                ),
                raw_output=raw,
                remedy_id="remedy_gpu_not_visible",
                risk="manual",
            )

        # ── build dir writable ────────────────────────────────────────────────

        if check_id == "build_dir_writable":
            if self._install_path is None:
                return CheckResult(
                    check_id=check_id,
                    passed=True,
                    message=(
                        "Install path not yet set — "
                        "will be checked after configuration."
                    ),
                    raw_output="",
                )
            if not self._install_path.exists():
                return CheckResult(
                    check_id=check_id,
                    passed=True,
                    message=(
                        f"Install path does not exist yet and will be created: "
                        f"{self._install_path}"
                    ),
                    raw_output="",
                )
            return directory_writable(
                check_id=check_id,
                path=self._install_path,
                remedy_id="remedy_build_dir_permissions",
                risk="manual",
            )

        return CheckResult(
            check_id=check_id,
            passed=False,
            message=f"Unknown check ID: '{check_id}'",
            raw_output="",
        )

    # ── Remedies ───────────────────────────────────────────────────────────────

    def run_remedy(self, remedy_id: str) -> RemedyResult:
        try:
            return self._dispatch_remedy(remedy_id)
        except Exception:
            return RemedyResult(
                remedy_id=remedy_id,
                success=False,
                message=f"Unexpected error running remedy '{remedy_id}'.",
                undo_instructions=(
                    "Check raw output and verify system state manually before retrying."
                ),
                raw_output=traceback.format_exc(),
            )

    def _dispatch_remedy(self, remedy_id: str) -> RemedyResult:

        # ── winget missing ────────────────────────────────────────────────────

        if remedy_id == "remedy_install_winget":
            # winget ships with App Installer from the Microsoft Store.
            # We can't install winget via winget — guide the user instead.
            result = RemedyResult(
                remedy_id=remedy_id,
                success=True,
                message="Instructions for installing winget shown to user.",
                undo_instructions="winget can be uninstalled via Apps & Features.",
                raw_output="",
                instructions=[
                    "Windows Package Manager (winget) is not installed.",
                    "",
                    "To install it:",
                    "  1. Open the Microsoft Store",
                    "  2. Search for 'App Installer'",
                    "  3. Click 'Get' or 'Update' — winget is included in this package",
                    "  4. Restart this wizard after installation",
                    "",
                    "Alternatively, on Windows 10/11 you can install it via:",
                    "  https://aka.ms/getwinget",
                    "",
                    "If you prefer not to install winget, AURINI will guide you",
                    "through manual installation of all remaining dependencies.",
                ],
            )
            self._log_remedy(result, RevertType.MANUAL,
                             revert_note=result.undo_instructions)
            return result

        # ── Visual Studio 2022 ────────────────────────────────────────────────

        if remedy_id == "remedy_install_vs2022":
            # VS2022 is too large and complex to install silently via winget
            # in a way that reliably selects the right workloads. Manual only.
            result = RemedyResult(
                remedy_id=remedy_id,
                success=True,
                message="Visual Studio 2022 installation instructions shown to user.",
                undo_instructions=(
                    "Visual Studio 2022 can be uninstalled via Apps & Features."
                ),
                raw_output="",
                instructions=[
                    "Visual Studio 2022 with the C++ workload is required.",
                    "",
                    "To install it:",
                    "  1. Download the installer from:",
                    "     https://visualstudio.microsoft.com/vs/community/",
                    "     (Community Edition is free)",
                    "",
                    "  2. Run the installer and select the workload:",
                    "     'Desktop development with C++'",
                    "",
                    "  3. Under Individual Components, also ensure these are ticked:",
                    "     - C++ CMake tools for Windows",
                    "     - MSVC v143 (or latest) build tools",
                    "",
                    "  4. Click Install and wait for it to complete (~5–15 GB download)",
                    "",
                    "  5. Restart this wizard after installation",
                    "",
                    "Note: Visual Studio Code is NOT the same as Visual Studio —",
                    "you need the full Visual Studio 2022 IDE.",
                ],
            )
            self._log_remedy(result, RevertType.MANUAL,
                             revert_note=result.undo_instructions)
            return result

        # ── oneAPI ────────────────────────────────────────────────────────────

        if remedy_id == "remedy_install_oneapi":
            result = RemedyResult(
                remedy_id=remedy_id,
                success=True,
                message="Intel oneAPI installation instructions shown to user.",
                undo_instructions=(
                    "Intel oneAPI can be uninstalled via Apps & Features. "
                    "Look for 'Intel oneAPI' entries."
                ),
                raw_output="",
                instructions=[
                    "Intel oneAPI is required for Intel Arc GPU support.",
                    "",
                    "Recommended: install Intel Deep Learning Essentials",
                    "(smaller download, includes everything needed):",
                    "",
                    "  1. Go to:",
                    "     https://www.intel.com/content/www/us/en/developer/tools/oneapi/base-toolkit.html",
                    "",
                    "  2. Click 'Get the Toolkit' and choose",
                    "     'Intel Deep Learning Essentials' (recommended)",
                    "     or 'Intel oneAPI Base Toolkit' (full install)",
                    "",
                    "  3. Run the installer. Keep the default installation path:",
                    r"     C:\Program Files (x86)\Intel\oneAPI",
                    "     (AURINI expects this location)",
                    "",
                    "  4. Restart this wizard after installation",
                ],
            )
            self._log_remedy(result, RevertType.MANUAL,
                             revert_note=result.undo_instructions)
            return result

        # ── Arc driver ────────────────────────────────────────────────────────

        if remedy_id == "remedy_install_arc_driver":
            min_str = _fmt_version(MIN_DRIVER_VERSION)
            result = RemedyResult(
                remedy_id=remedy_id,
                success=True,
                message="Intel Arc driver installation instructions shown to user.",
                undo_instructions=(
                    "Drivers can be rolled back via Device Manager → "
                    "Display Adapters → Intel Arc → Driver → Roll Back Driver."
                ),
                raw_output="",
                instructions=[
                    f"An Intel Arc GPU driver version {min_str} or newer is required.",
                    "",
                    "To update your driver:",
                    "  Option A — Intel Arc Control (recommended if already installed):",
                    "    Open Intel Arc Control → Driver → Check for Updates",
                    "",
                    "  Option B — Manual download:",
                    "    1. Go to https://www.intel.com/content/www/us/en/download-center/home.html",
                    "    2. Search for 'Intel Arc Graphics Driver'",
                    "    3. Download and run the installer for your GPU",
                    "",
                    "  Option C — Windows Update:",
                    "    Settings → Windows Update → Advanced Options → Optional Updates",
                    "    Intel driver updates sometimes appear here",
                    "",
                    "After updating, restart your computer, then continue this wizard.",
                ],
            )
            self._log_remedy(result, RevertType.MANUAL,
                             revert_note=result.undo_instructions)
            return result

        # ── git ───────────────────────────────────────────────────────────────

        if remedy_id == "remedy_install_git":
            return self._winget_install(
                remedy_id=remedy_id,
                package_id=WINGET_PACKAGES["git"],
                display_name="Git for Windows",
                manual_url="https://git-scm.com/download/win",
                manual_instructions=[
                    "  1. Download the installer from https://git-scm.com/download/win",
                    "  2. Run the installer with default settings",
                    "  3. Restart this wizard (git must be on PATH)",
                ],
                undo_instructions=(
                    "Git can be uninstalled via Apps & Features, "
                    "or: winget uninstall Git.Git"
                ),
            )

        # ── cmake ─────────────────────────────────────────────────────────────

        if remedy_id == "remedy_install_cmake":
            return self._winget_install(
                remedy_id=remedy_id,
                package_id=WINGET_PACKAGES["cmake"],
                display_name="CMake",
                manual_url="https://cmake.org/download/",
                manual_instructions=[
                    "  1. Download the Windows x64 installer from https://cmake.org/download/",
                    "  2. Run the installer — tick 'Add CMake to the system PATH'",
                    "  3. Restart this wizard",
                ],
                undo_instructions=(
                    "CMake can be uninstalled via Apps & Features, "
                    "or: winget uninstall Kitware.CMake"
                ),
            )

        # ── ninja ─────────────────────────────────────────────────────────────

        if remedy_id == "remedy_install_ninja":
            return self._winget_install(
                remedy_id=remedy_id,
                package_id=WINGET_PACKAGES["ninja"],
                display_name="Ninja build system",
                manual_url="https://github.com/ninja-build/ninja/releases",
                manual_instructions=[
                    "  1. Download ninja-win.zip from",
                    "     https://github.com/ninja-build/ninja/releases",
                    "  2. Extract ninja.exe to a folder on your PATH",
                    "     (e.g. C:\\Windows\\System32, or add a new PATH entry)",
                    "  3. Restart this wizard",
                ],
                undo_instructions=(
                    "Ninja can be uninstalled via Apps & Features, "
                    "or: winget uninstall Ninja-build.Ninja"
                ),
            )

        # ── GPU not visible ───────────────────────────────────────────────────

        if remedy_id == "remedy_gpu_not_visible":
            result = RemedyResult(
                remedy_id=remedy_id,
                success=True,
                message="GPU visibility instructions shown to user. User chose to continue.",
                undo_instructions="No system changes were made by this remedy.",
                raw_output="",
                instructions=[
                    "No Intel Arc GPU was detected via sycl-ls.",
                    "",
                    "Common causes and fixes:",
                    "",
                    "  1. Driver version too old",
                    "     → Update your Arc driver (see arc_driver_present check above)",
                    "",
                    "  2. OpenCL / Vulkan Compatibility Pack blocking detection",
                    "     → This package is sometimes installed silently alongside Arc drivers.",
                    "     → Open the Microsoft Store, search 'OpenCL and Vulkan Compatibility Pack',",
                    "       and uninstall it if present, then recheck.",
                    "",
                    "  3. setvars.bat did not activate correctly",
                    "     → Ensure oneAPI was installed to the default path and try again.",
                    "",
                    "  4. Multiple GPUs (integrated + discrete)",
                    "     → Both may appear in sycl-ls output. This is normal.",
                    "     → llama.cpp will use ONEAPI_DEVICE_SELECTOR=level_zero:0",
                    "       to target the discrete Arc GPU at launch.",
                    "",
                    "You can continue the build without confirming GPU visibility,",
                    "but llama.cpp may not use your GPU when you try to run it.",
                ],
            )
            self._log_remedy(result, RevertType.MANUAL,
                             revert_note=result.undo_instructions)
            return result

        # ── build dir permissions ─────────────────────────────────────────────

        if remedy_id == "remedy_build_dir_permissions":
            path = str(self._install_path) if self._install_path else "{install_path}"
            result = RemedyResult(
                remedy_id=remedy_id,
                success=True,
                message="Build directory permissions instructions shown to user.",
                undo_instructions="No system changes were made by this remedy.",
                raw_output="",
                instructions=[
                    f"The install directory is not writable: {path}",
                    "",
                    "To fix this:",
                    f"  1. Right-click the folder: {path}",
                    "  2. Select Properties → Security → Edit",
                    "  3. Select your user account and tick 'Full Control'",
                    "  4. Click Apply → OK",
                    "",
                    "Alternatively, choose a different install path that you",
                    "already own (e.g. inside your user folder).",
                ],
            )
            self._log_remedy(result, RevertType.MANUAL,
                             revert_note=result.undo_instructions)
            return result

        return RemedyResult(
            remedy_id=remedy_id,
            success=False,
            message=f"Unknown remedy ID: '{remedy_id}'",
            undo_instructions="No changes were made.",
            raw_output="",
        )

    # ── Build ──────────────────────────────────────────────────────────────────

    def cmake_flags(self, build_config: dict[str, Any]) -> list[str]:
        flags = CMAKE_FLAGS_BASE.copy()
        fp16  = build_config.get("fp16", True)
        flags.append(f"-DGGML_SYCL_F16={'ON' if fp16 else 'OFF'}")
        return flags

    def env_setup_script(self) -> str | None:
        """
        On Windows, env setup is not a script we can source — it's handled
        by _capture_setvars_env() which injects the environment directly into
        subprocesses. The build runner must use build_env() instead of this
        method on Windows.

        We return None here so shared.run_build() doesn't try to `source`
        anything. The Windows build flow must call run_build_windows() instead.
        """
        return None

    def build_env(self) -> dict[str, str] | None:
        """
        Return the setvars.bat environment for use in build subprocesses.

        Returns None if oneAPI is not installed or env capture fails.
        This is the Windows-specific equivalent of `source setvars.sh`.
        """
        return _capture_setvars_env()

    # ── Launch ─────────────────────────────────────────────────────────────────

    def build_launch_env(self, base_env: dict[str, str]) -> dict[str, str]:
        """
        Return the environment dict to use when launching llama-server.

        On Windows we inject the full setvars.bat environment so the
        oneAPI runtime DLLs are on PATH. We also set ONEAPI_DEVICE_SELECTOR
        to ensure the discrete Arc GPU is used when both iGPU and dGPU
        are present.
        """
        # Start with the setvars env if available, fall back to base_env.
        env = _capture_setvars_env() or dict(base_env)

        # Target the discrete GPU (level_zero:0). Users with only an iGPU
        # will also see level_zero:0, so this is safe for both configurations.
        env["ONEAPI_DEVICE_SELECTOR"] = "level_zero:0"

        # Persistent SYCL kernel cache — major speedup on second and subsequent
        # launches, as compiled kernels are reused rather than recompiled.
        env["SYCL_CACHE_PERSISTENT"] = "1"

        return env

    def binary_path(self, install_path: Path) -> Path:
        return install_path / BINARY_RELATIVE

    # ── Private helpers ────────────────────────────────────────────────────────

    def _detect_vs2022(self) -> tuple[bool, str]:
        """
        Detect Visual Studio 2022 with C++ workload.

        First tries vswhere.exe (ships with VS 2017+), which is the reliable
        way to find VS installs. Falls back to checking the default install
        path if vswhere is not found.

        Returns (found: bool, raw_output: str).
        """
        vswhere = Path(
            r"C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe"
        )

        if vswhere.exists():
            try:
                result = subprocess.run(
                    [
                        str(vswhere),
                        "-version", "[17,)",  # VS2022+ (17.x) and VS2026+ (18.x)
                        "-requires", "Microsoft.VisualCpp.Tools.HostX64.TargetX64",
                        "-property", "installationPath",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    encoding="utf-8",
                    errors="replace",
                )
                raw = (result.stdout + result.stderr).strip()
                if result.returncode == 0 and result.stdout.strip():
                    return True, raw
                return False, raw
            except Exception:
                pass  # Fall through to path check

        # Fallback: check default VS2022 Community path for cl.exe
        cl_path = Path(
            r"C:\Program Files\Microsoft Visual Studio\2022\Community"
            r"\VC\Tools\MSVC"
        )
        if cl_path.exists():
            # Look for any cl.exe under the MSVC tools directory
            for cl in cl_path.rglob("cl.exe"):
                if "x64" in str(cl):
                    return True, f"Found cl.exe at: {cl}"

        return False, "VS2022 not found via vswhere or default path."

    def _winget_install(
        self,
        remedy_id:           str,
        package_id:          str,
        display_name:        str,
        manual_url:          str,
        manual_instructions: list[str],
        undo_instructions:   str,
    ) -> RemedyResult:
        """
        Install a package via winget, or fall back to manual instructions.

        If winget is not available (self._winget_available is False), returns
        a manual remedy result with clear download instructions instead.
        Winget availability is cached from the winget_present check.
        """
        # If we haven't checked for winget yet, check now.
        if self._winget_available is None:
            import shutil as _shutil
            self._winget_available = _shutil.which("winget") is not None

        if not self._winget_available:
            result = RemedyResult(
                remedy_id=remedy_id,
                success=True,
                message=f"Manual installation instructions for {display_name} shown.",
                undo_instructions=undo_instructions,
                raw_output="",
                instructions=[
                    f"{display_name} is not installed, and winget is not available",
                    "for automatic installation.",
                    "",
                    f"Please install {display_name} manually:",
                    *manual_instructions,
                    "",
                    f"Download from: {manual_url}",
                    "",
                    "After installing, restart this wizard to continue.",
                ],
            )
            self._log_remedy(result, RevertType.MANUAL,
                             revert_note=result.undo_instructions)
            return result

        # winget is available — run the install.
        code, raw = shared.run(
            ["winget", "install", "--id", package_id,
             "--silent", "--accept-package-agreements",
             "--accept-source-agreements"],
            timeout=300,
        )

        success = code == 0
        result = RemedyResult(
            remedy_id=remedy_id,
            success=success,
            message=(
                f"Installed {display_name} via winget." if success
                else (
                    f"winget install of {display_name} failed. "
                    f"You can install it manually from: {manual_url}"
                )
            ),
            undo_instructions=undo_instructions,
            raw_output=raw,
        )
        self._log_remedy(
            result,
            RevertType.AUTO,
            revert_command=["winget", "uninstall", "--id", package_id],
        )
        return result

    def _find_cmake_windows(self) -> Path | None:
        """
        Find cmake.exe on Windows, including VS-internal install locations
        that are not on the system PATH.

        Search order:
        1. System PATH (shutil.which)
        2. Known VS cmake locations
        3. cmake standalone install default path
        """
        import shutil as _shutil

        # 1. System PATH
        found = _shutil.which("cmake")
        if found:
            return Path(found)

        # 2. Visual Studio internal cmake (installed via VS Installer)
        vs_roots = [
            Path(r"C:\Program Files\Microsoft Visual Studio\2022\Community"),
            Path(r"C:\Program Files\Microsoft Visual Studio\2022\Professional"),
            Path(r"C:\Program Files\Microsoft Visual Studio\2022\Enterprise"),
            Path(r"C:\Program Files\Microsoft Visual Studio\2026\Community"),
            Path(r"C:\Program Files\Microsoft Visual Studio\2026\Professional"),
            Path(r"C:\Program Files\Microsoft Visual Studio\2026\Enterprise"),
            Path(r"C:\Program Files\Microsoft Visual Studio\18\Community"),
            Path(r"C:\Program Files\Microsoft Visual Studio\18\Professional"),
            Path(r"C:\Program Files\Microsoft Visual Studio\18\Enterprise"),
        ]
        for root in vs_roots:
            cmake = root / r"Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
            if cmake.exists():
                return cmake

        # 3. cmake standalone default install path
        standalone = Path(r"C:\Program Files\CMake\bin\cmake.exe")
        if standalone.exists():
            return standalone

        return None

    def _log_remedy(
        self,
        result:         RemedyResult,
        revert_type:    RevertType,
        revert_command: list[str] | None = None,
        revert_note:    str | None = None,
    ) -> None:
        """Log a remedy result to the job log if one is set."""
        if self._job_log is None:
            return
        self._job_log.record_remedy(
            result=result,
            revert_type=revert_type,
            revert_command=revert_command,
            revert_note=revert_note,
        )
