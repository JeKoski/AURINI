"""
plugins/llama-cpp/plugin.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~
AURINI plugin for llama.cpp — dispatcher and shared flow.

This file stays thin. It:
  1. Detects the current platform and GPU vendor
  2. Selects the right backend
  3. Delegates all hardware-specific work to the backend
  4. Handles the shared flow (clone/pull, backup, build, uninstall, launch)
     by combining the backend's cmake flags / env with shared.py helpers

Adding a new backend:
  1. Create backends/<n>.py subclassing LlamaCppBackend
  2. Register it in _select_backend() below
  3. Done — nothing else here changes

Usage (by the core):

    from plugins.llama_cpp import plugin as llama_plugin
    p = llama_plugin.load(install_path="/home/alice/llama.cpp")
    p.set_job_log(job_log)

    for check_id in p.get_checks():
        result = p.run_check(check_id)
        ...

    p.install(config)
"""

from __future__ import annotations

import os
import platform
import shutil
import traceback
from pathlib import Path
from typing import Any

from aurini.core.base import AuriniPlugin, CheckResult, RemedyResult
from aurini.core.log import JobLog, RevertType
from plugins.llama_cpp.backends import shared
from plugins.llama_cpp.backends.base import LlamaCppBackend


# ── Backend registry ───────────────────────────────────────────────────────────

def _select_backend(
    install_path: Path | None,
    job_log:      JobLog | None,
) -> LlamaCppBackend:
    """
    Detect the current platform and GPU vendor and return the right backend.

    Detection order:
    1. OS (Linux / Windows / macOS)
    2. GPU vendor (Intel / NVIDIA / AMD / Apple / unknown)

    Raises RuntimeError if the OS is unsupported or no backend exists yet
    for the detected combination. Future backends are registered here.
    """
    system = platform.system()

    if system == "Linux":
        vendor = _detect_gpu_vendor_linux()
        if vendor == "intel":
            from plugins.llama_cpp.backends.sycl_linux import SyclLinuxBackend
            return SyclLinuxBackend(install_path=install_path, job_log=job_log)
        # Future: NVIDIA → CudaLinuxBackend, AMD → RocmLinuxBackend
        raise RuntimeError(
            f"No llama.cpp backend available for GPU vendor '{vendor}' on Linux yet.\n"
            "Currently supported: Intel Arc (SYCL).\n"
            "NVIDIA (CUDA) and AMD (ROCm) support is coming."
        )

    if system == "Windows":
        # Future: SyclWindowsBackend, CudaWindowsBackend
        raise RuntimeError(
            "Windows support for llama.cpp is not yet implemented in AURINI.\n"
            "It is planned — check back soon."
        )

    if system == "Darwin":
        # Future: MetalBackend
        raise RuntimeError(
            "macOS (Metal) support for llama.cpp is not yet implemented in AURINI.\n"
            "It is planned — check back soon."
        )

    raise RuntimeError(f"Unsupported operating system: {system}")


def _detect_gpu_vendor_linux() -> str:
    """
    Detect the primary GPU vendor on Linux via lspci.

    Returns: "intel" | "nvidia" | "amd" | "unknown"
    Checks VGA, 3D controller, and display controller lines.
    NVIDIA and AMD are checked before Intel to correctly identify discrete
    GPUs on systems that also have Intel integrated graphics.
    """
    try:
        import subprocess
        result = subprocess.run(
            ["lspci"], capture_output=True, text=True, timeout=10
        )
        output = result.stdout.lower()

        for line in output.splitlines():
            if "vga" in line or "3d" in line or "display" in line:
                if "nvidia" in line:
                    return "nvidia"
                if "amd" in line or "radeon" in line or "advanced micro" in line:
                    return "amd"
                if "intel" in line:
                    return "intel"

        return "unknown"
    except Exception:
        return "unknown"


# ── Plugin ─────────────────────────────────────────────────────────────────────

class Plugin(AuriniPlugin):
    """
    llama.cpp plugin — thin dispatcher over hardware-specific backends.

    Use Plugin.load() for auto-detection, or instantiate directly with a
    specific backend for testing.
    """

    def __init__(
        self,
        backend:      LlamaCppBackend,
        install_path: Path | None = None,
    ) -> None:
        self._backend      = backend
        self._install_path = install_path
        self._job_log: JobLog | None = None

    # ── Factory ────────────────────────────────────────────────────────────────

    @classmethod
    def load(
        cls,
        install_path: str | Path | None = None,
    ) -> Plugin:
        """
        Auto-detect the platform and GPU, select the right backend, and
        return a configured Plugin ready for use.

        install_path may be None at load time and set later via
        set_install_path() once the user has chosen a location.
        """
        path    = Path(install_path).expanduser().resolve() if install_path else None
        backend = _select_backend(install_path=path, job_log=None)
        return cls(backend=backend, install_path=path)

    # ── Identity ───────────────────────────────────────────────────────────────

    @property
    def plugin_id(self) -> str:
        return "llama-cpp"

    @property
    def display_name(self) -> str:
        return f"llama.cpp  [{self._backend.display_name}]"

    @property
    def backend_id(self) -> str:
        return self._backend.backend_id

    # ── Configuration ──────────────────────────────────────────────────────────

    def set_job_log(self, job_log: JobLog) -> None:
        """Inject the active JobLog. Call before any system-modifying method."""
        self._job_log          = job_log
        self._backend._job_log = job_log

    def set_install_path(self, path: str | Path) -> None:
        """Set or update the install path after construction."""
        resolved                    = Path(path).expanduser().resolve()
        self._install_path          = resolved
        self._backend._install_path = resolved

    # ── Pre-flight checks ──────────────────────────────────────────────────────

    def get_checks(self) -> list[str]:
        return self._backend.get_checks()

    def run_check(self, check_id: str) -> CheckResult:
        return self._backend.run_check(check_id)

    # ── Remedies ───────────────────────────────────────────────────────────────

    def run_remedy(self, remedy_id: str) -> RemedyResult:
        return self._backend.run_remedy(remedy_id)

    # ── Install ────────────────────────────────────────────────────────────────

    def install(self, config: dict[str, Any]) -> None:
        """
        Clone llama.cpp and build it using the active backend's cmake flags.

        config keys:
            install_path (str)  — where to clone
            cores (int)         — parallel build jobs
            fp16 (bool)         — FP16 compute (default True)
            + any backend-specific keys

        Raises RuntimeError on failure. All actions logged to JobLog.
        """
        self._require_job_log("install")

        install_path = self._resolve_install_path(config)
        cores        = int(config.get("cores", shared.default_build_cores()))
        flags        = self._backend.cmake_flags(config)
        env_setup    = self._backend.env_setup_script()

        success, raw = shared.clone(install_path)
        if not success:
            raise RuntimeError(
                f"git clone failed. Check your internet connection.\n\n{raw}"
            )
        self._log_action(
            description=f"Cloned llama.cpp from GitHub into: {install_path}",
            revert_type=RevertType.AUTO,
            revert_command=["rm", "-rf", str(install_path)],
            revert_note=f"This will permanently delete {install_path}.",
            raw_output=raw,
        )

        self._build(install_path, flags, cores, env_setup, config)

    # ── Update ─────────────────────────────────────────────────────────────────

    def update(self, config: dict[str, Any]) -> None:
        """
        Back up modified files, pull latest changes, and rebuild.

        config keys:
            cores (int)  — parallel build jobs
            fp16 (bool)  — FP16 compute (default True)

        Raises RuntimeError on failure.
        """
        self._require_job_log("update")
        self._require_install_path("update")

        cores     = int(config.get("cores", shared.default_build_cores()))
        flags     = self._backend.cmake_flags(config)
        env_setup = self._backend.env_setup_script()

        backup_dir, untracked, modified = shared.backup_modified_files(self._install_path)
        if backup_dir:
            total = untracked + modified
            self._log_action(
                description=(
                    f"Backed up {total} file(s) to {backup_dir} before update. "
                    f"({modified} modified repo file(s), {untracked} untracked)"
                ),
                revert_type=RevertType.MANUAL,
                revert_note=(
                    f"Your files are in: {backup_dir}\n"
                    "Copy them back manually if needed. "
                    "Delete the folder once satisfied everything is working."
                ),
                raw_output="",
            )

        success, raw = shared.pull(self._install_path)
        if not success:
            raise RuntimeError(
                f"git pull failed. "
                f"{'Files backed up at: ' + str(backup_dir) if backup_dir else ''}\n\n{raw}"
            )
        self._log_action(
            description="Pulled latest llama.cpp changes from GitHub.",
            revert_type=RevertType.MANUAL,
            revert_note=(
                "To revert to the previous commit:\n"
                f"  cd {self._install_path}\n"
                "  git log  (find the previous commit hash)\n"
                "  git reset --hard <previous-hash>"
            ),
            raw_output=raw,
        )

        self._build(self._install_path, flags, cores, env_setup, config)

    # ── Uninstall ──────────────────────────────────────────────────────────────

    def uninstall(self) -> None:
        """
        Remove the llama.cpp install directory.

        Refuses to delete a directory that doesn't look like a llama.cpp repo.
        """
        self._require_job_log("uninstall")
        self._require_install_path("uninstall")

        if not self._install_path.exists():
            return

        if not shared.is_llama_repo(self._install_path):
            raise RuntimeError(
                f"Safety check failed: {self._install_path} does not look like a "
                "llama.cpp repository (.git and CMakeLists.txt not found).\n"
                "AURINI will not delete it. Remove it manually if you are sure."
            )

        shutil.rmtree(str(self._install_path))
        self._log_action(
            description=f"Removed llama.cpp install directory: {self._install_path}",
            revert_type=RevertType.MANUAL,
            revert_note=(
                f"To restore:\n"
                f"  git clone {shared.LLAMA_CPP_REPO} {self._install_path}\n"
                "Then rebuild with your previous build settings."
            ),
            raw_output="",
        )

    # ── Launch ─────────────────────────────────────────────────────────────────

    def build_launch_command(self, profile: dict[str, Any]) -> list[str]:
        """
        Construct the argv list to launch llama-server from a profile.
        Use build_launch_env() to get the correct environment dict.
        """
        self._require_install_path("build_launch_command")

        binary   = str(self._backend.binary_path(self._install_path))
        argv     = [binary]
        settings = profile.get("settings", {})

        def add(flag: str, value: Any = None) -> None:
            argv.append(flag)
            if value is not None:
                argv.append(str(value))

        def enabled(key: str) -> bool:
            return settings.get(key, {}).get("enabled", False)

        def val(key: str) -> Any:
            return settings.get(key, {}).get("value")

        if enabled("model_path") and val("model_path"):
            add("--model", val("model_path"))
        if enabled("ctx_size") and val("ctx_size") is not None:
            add("--ctx-size", val("ctx_size"))
        if enabled("gpu_layers") and val("gpu_layers") is not None:
            add("--gpu-layers", val("gpu_layers"))
        if enabled("flash_attn") and val("flash_attn"):
            add("--flash-attn")
        if enabled("host") and val("host"):
            add("--host", val("host"))
        if enabled("port") and val("port") is not None:
            add("--port", val("port"))
        if enabled("threads") and val("threads") is not None:
            add("--threads", val("threads"))
        else:
            add("--threads", shared.default_build_cores())

        for custom in profile.get("custom_args", []):
            if custom.get("enabled"):
                argv.append(custom["flag"])
                if custom.get("value"):
                    argv.append(str(custom["value"]))

        return argv

    def build_launch_env(self, base_env: dict[str, str] | None = None) -> dict[str, str]:
        """
        Return the environment dict to pass when launching llama-server.
        Delegates to the backend for hardware-specific variables.
        """
        return self._backend.build_launch_env(base_env or dict(os.environ))

    # ── Private: build ─────────────────────────────────────────────────────────

    def _build(
        self,
        install_path: Path,
        flags:        list[str],
        cores:        int,
        env_setup:    str | None,
        config:       dict[str, Any],
    ) -> None:
        success, note = shared.run_build(
            repo=install_path,
            cmake_flags=flags,
            cores=cores,
            env_setup=env_setup,
        )
        if not success:
            raise RuntimeError(
                "Build failed. Check the output above for the specific error.\n"
                "Common causes:\n"
                "  · GPU toolkit not fully installed\n"
                "  · Compiler not on PATH after sourcing environment\n"
                "  · Insufficient disk space\n"
                "  · Try reducing parallel jobs"
            )

        binary = self._backend.binary_path(install_path)
        if not binary.exists():
            raise RuntimeError(
                f"Build appeared to succeed but binary not found at: {binary}\n"
                "Check build output for errors."
            )

        fp16 = config.get("fp16", True)
        self._log_action(
            description=(
                f"Built llama.cpp — backend: {self._backend.display_name}, "
                f"fp16={'ON' if fp16 else 'OFF'}, jobs={cores}."
            ),
            revert_type=RevertType.MANUAL,
            revert_note=(
                f"To remove the build output:\n"
                f"  rm -rf {install_path / 'build'}\n"
                "Source files remain. Run cmake again to rebuild."
            ),
            raw_output=note,
        )

    # ── Private: log ───────────────────────────────────────────────────────────

    def _log_action(
        self,
        description:    str,
        revert_type:    RevertType,
        raw_output:     str,
        revert_command: list[str] | None = None,
        revert_note:    str | None = None,
    ) -> None:
        if self._job_log is None:
            return
        self._job_log.add_entry(
            description=description,
            revert_type=revert_type,
            revert_command=revert_command,
            revert_note=revert_note,
            raw_output=raw_output,
        )

    # ── Private: guards ────────────────────────────────────────────────────────

    def _require_job_log(self, method: str) -> None:
        if self._job_log is None:
            raise RuntimeError(
                f"Plugin.{method}() called without a JobLog. "
                "Call set_job_log(job_log) before any system-modifying method."
            )

    def _require_install_path(self, method: str) -> None:
        if self._install_path is None:
            raise RuntimeError(
                f"Plugin.{method}() called without an install path. "
                "Call set_install_path() or pass install_path to Plugin.load()."
            )

    def _resolve_install_path(self, config: dict[str, Any]) -> Path:
        if "install_path" in config:
            path = Path(config["install_path"]).expanduser().resolve()
            self.set_install_path(path)
            return path
        if self._install_path:
            return self._install_path
        raise RuntimeError(
            "install_path must be provided in config or set via set_install_path()."
        )


# ── Convenience loader ─────────────────────────────────────────────────────────

def load(install_path: str | Path | None = None) -> Plugin:
    """
    Module-level convenience function. Equivalent to Plugin.load().

        from plugins.llama_cpp import plugin as llama_plugin
        p = llama_plugin.load(install_path='~/llama.cpp')
    """
    return Plugin.load(install_path=install_path)
