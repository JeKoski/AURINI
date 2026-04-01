"""
plugins/llama-cpp/backends/sycl_linux.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
llama.cpp backend for Intel Arc GPUs on Linux via Intel oneAPI (SYCL).

Tested hardware: Intel Arc A750 (8GB VRAM), Ubuntu 22.04+.
Covers all Arc discrete GPUs and Intel integrated GPUs with level_zero support.

cmake flags used:
    -DGGML_SYCL=ON
    -DGGML_SYCL_TARGET=INTEL
    -DGGML_SYCL_DNN=ON
    -DGGML_SYCL_GRAPH=ON
    -DGGML_SYCL_F16=ON/OFF       (from build config, default ON)
    -DCMAKE_BUILD_TYPE=Release
    -DCMAKE_C_COMPILER=icx       (must be explicit — see note below)
    -DCMAKE_CXX_COMPILER=icpx

icx/icpx must be declared explicitly even after sourcing setvars.sh.
Without them cmake may silently fall back to gcc/g++ and produce a build
that appears to succeed but has no SYCL support.
"""

from __future__ import annotations

import os
import traceback
from pathlib import Path
from typing import Any

from aurini.core.base import CheckResult, RemedyResult
from aurini.core.checks import (
    command_exists,
    disk_space_gte,
    directory_writable,
    file_exists,
    gpu_visible,
    host_reachable,
    internet_reachable,
    user_in_group,
)
from aurini.core.log import RevertType
from plugins.llama_cpp.backends.base import LlamaCppBackend
from plugins.llama_cpp.backends import shared


# ── Constants ──────────────────────────────────────────────────────────────────

ONEAPI_SETVARS = Path("/opt/intel/oneapi/setvars.sh")
BINARY_RELATIVE = "build/bin/llama-server"

CMAKE_FLAGS_BASE = [
    "-DGGML_SYCL=ON",
    "-DGGML_SYCL_TARGET=INTEL",
    "-DGGML_SYCL_DNN=ON",
    "-DGGML_SYCL_GRAPH=ON",
    "-DCMAKE_BUILD_TYPE=Release",
    "-DCMAKE_C_COMPILER=icx",
    "-DCMAKE_CXX_COMPILER=icpx",
]


# ── Backend ────────────────────────────────────────────────────────────────────

class SyclLinuxBackend(LlamaCppBackend):
    """
    Intel Arc GPU backend for llama.cpp on Linux.

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

    # ── Identity ───────────────────────────────────────────────────────────────

    @property
    def backend_id(self) -> str:
        return "sycl_linux"

    @property
    def display_name(self) -> str:
        return "Intel Arc (SYCL) — Linux"

    # ── Pre-flight checks ──────────────────────────────────────────────────────

    def get_checks(self) -> list[str]:
        """
        Check order matters:
        - oneapi_present before gpu_visible (sycl-ls needs oneAPI sourced)
        - render/video groups before gpu_visible (missing groups are the most
          common cause of GPU visibility failures — fix them first)
        - disk and network before install steps
        - build_dir_writable last (path may not be set yet at check time)
        """
        return [
            "oneapi_present",
            "git_present",
            "cmake_present",
            "render_group",
            "video_group",
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
        if check_id == "oneapi_present":
            return file_exists(
                check_id=check_id,
                path=ONEAPI_SETVARS,
                message_found=f"Intel oneAPI found at: {ONEAPI_SETVARS}",
                message_missing=(
                    "Intel oneAPI was not found. It is required to build llama.cpp "
                    "with Intel Arc GPU support."
                ),
                remedy_id="remedy_install_oneapi",
                risk="manual",
            )

        if check_id == "git_present":
            return command_exists(
                check_id=check_id,
                command="git",
                remedy_id="remedy_install_git",
                risk="high",
            )

        if check_id == "cmake_present":
            return command_exists(
                check_id=check_id,
                command="cmake",
                remedy_id="remedy_install_cmake",
                risk="high",
            )

        if check_id == "render_group":
            return user_in_group(
                check_id=check_id,
                group="render",
                remedy_id="remedy_add_render_group",
                risk="high",
            )

        if check_id == "video_group":
            return user_in_group(
                check_id=check_id,
                group="video",
                remedy_id="remedy_add_video_group",
                risk="high",
            )

        if check_id == "disk_space":
            path = self._install_path or Path.home()
            return disk_space_gte(
                check_id=check_id,
                path=path,
                minimum_gb=5.0,
            )

        if check_id == "internet_reachable":
            return internet_reachable(check_id=check_id)

        if check_id == "github_reachable":
            return host_reachable(
                check_id=check_id,
                host="github.com",
                port=443,
            )

        if check_id == "gpu_visible":
            return gpu_visible(
                check_id=check_id,
                command=["sycl-ls"],
                expected="level_zero:gpu",
                message_found="Intel Arc GPU detected via level_zero — ready to build.",
                message_missing=(
                    "No Intel Arc GPU detected via sycl-ls. "
                    "Check GPU drivers and group membership. "
                    "You can continue, but llama.cpp may not use your GPU at runtime."
                ),
                env_setup_command=(
                    f"source {ONEAPI_SETVARS} --force intel64 2>/dev/null"
                ),
                remedy_id="remedy_gpu_not_visible",
                risk="manual",
            )

        if check_id == "build_dir_writable":
            if self._install_path is None:
                return CheckResult(
                    check_id=check_id,
                    passed=True,
                    message="Install path not yet set — will be checked after configuration.",
                    raw_output="",
                )
            if not self._install_path.exists():
                return CheckResult(
                    check_id=check_id,
                    passed=True,
                    message=f"Install path does not exist yet and will be created: {self._install_path}",
                    raw_output="",
                )
            return directory_writable(
                check_id=check_id,
                path=self._install_path,
                remedy_id="remedy_build_dir_ownership",
                risk="high",
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
        username = os.environ.get("USER") or os.environ.get("LOGNAME") or "$USER"

        if remedy_id == "remedy_install_git":
            return self._apt_install("remedy_install_git", "git",
                                     "Install git via apt.",
                                     "sudo apt remove git")

        if remedy_id == "remedy_install_cmake":
            return self._apt_install("remedy_install_cmake", "cmake",
                                     "Install cmake via apt.",
                                     "sudo apt remove cmake")

        if remedy_id == "remedy_add_render_group":
            return self._add_user_to_group("remedy_add_render_group", "render", username)

        if remedy_id == "remedy_add_video_group":
            return self._add_user_to_group("remedy_add_video_group", "video", username)

        if remedy_id == "remedy_build_dir_ownership":
            path = str(self._install_path) if self._install_path else "{install_path}"
            code, raw = shared.run(["sudo", "chown", "-R", f"{username}:{username}", path])
            success = code == 0
            result = RemedyResult(
                remedy_id=remedy_id,
                success=success,
                message=(
                    f"Fixed ownership of {path} — now owned by {username}."
                    if success else
                    f"Failed to fix ownership of {path}. Check raw output."
                ),
                undo_instructions=f"sudo chown -R root:root {path}",
                raw_output=raw,
            )
            self._log_remedy(result, RevertType.AUTO,
                             revert_command=["sudo", "chown", "-R", "root:root", path])
            return result

        if remedy_id == "remedy_install_oneapi":
            result = RemedyResult(
                remedy_id=remedy_id,
                success=True,
                message="Intel oneAPI installation instructions shown to user.",
                undo_instructions=(
                    "To remove intel-deep-learning-essentials:\n"
                    "  sudo apt remove intel-deep-learning-essentials\n"
                    "  sudo apt autoremove"
                ),
                raw_output="",
            )
            self._log_remedy(result, RevertType.MANUAL,
                             revert_note=result.undo_instructions)
            return result

        if remedy_id == "remedy_gpu_not_visible":
            result = RemedyResult(
                remedy_id=remedy_id,
                success=True,
                message="GPU visibility instructions shown to user. User chose to continue.",
                undo_instructions="No system changes were made by this remedy.",
                raw_output="",
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
        return f"source {ONEAPI_SETVARS} --force intel64"

    # ── Launch ─────────────────────────────────────────────────────────────────

    def build_launch_env(self, base_env: dict[str, str]) -> dict[str, str]:
        """
        Add ONEAPI_DEVICE_SELECTOR to ensure llama-server uses the Arc GPU.

        Pattern from SENNI's server.py — without this env var, llama-server
        may not select the Intel GPU even with a SYCL build.
        """
        env = dict(base_env)
        env["ONEAPI_DEVICE_SELECTOR"] = "level_zero:gpu"
        return env

    def binary_path(self, install_path: Path) -> Path:
        return install_path / BINARY_RELATIVE

    # ── Private helpers ────────────────────────────────────────────────────────

    def _apt_install(
        self,
        remedy_id:         str,
        package:           str,
        description:       str,
        undo_instructions: str,
    ) -> RemedyResult:
        code, raw = shared.run(["sudo", "apt", "install", "-y", package])
        success = code == 0
        result = RemedyResult(
            remedy_id=remedy_id,
            success=success,
            message=(
                f"Installed {package} via apt." if success
                else f"Failed to install {package} via apt. Check raw output."
            ),
            undo_instructions=undo_instructions,
            raw_output=raw,
        )
        self._log_remedy(result, RevertType.AUTO,
                         revert_command=["sudo", "apt", "remove", package])
        return result

    def _add_user_to_group(
        self,
        remedy_id: str,
        group:     str,
        username:  str,
    ) -> RemedyResult:
        code, raw = shared.run(["sudo", "usermod", "-aG", group, username])
        success = code == 0
        result = RemedyResult(
            remedy_id=remedy_id,
            success=success,
            message=(
                f"Added '{username}' to group '{group}'. "
                "Log out and back in for the change to take effect."
                if success else
                f"Failed to add '{username}' to group '{group}'. Check raw output."
            ),
            undo_instructions=(
                f"sudo gpasswd -d {username} {group}\n"
                "Then log out and back in for the change to take effect."
            ),
            raw_output=raw,
        )
        self._log_remedy(
            result,
            RevertType.AUTO,
            revert_command=["sudo", "gpasswd", "-d", username, group],
            revert_note="Log out and back in after running this command.",
        )
        return result

    def _log_remedy(
        self,
        result:         RemedyResult,
        revert_type:    RevertType,
        revert_command: list[str] | None = None,
        revert_note:    str | None = None,
    ) -> None:
        if self._job_log is None:
            return
        self._job_log.add_entry(
            description=result.message,
            revert_type=revert_type,
            revert_command=revert_command,
            revert_note=revert_note or result.undo_instructions,
            raw_output=result.raw_output,
        )
