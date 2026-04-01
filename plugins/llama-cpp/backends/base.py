"""
plugins/llama-cpp/backends/base.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Abstract base class for llama.cpp build backends.

Each backend handles one specific combination of GPU vendor + OS:
    sycl_linux.py   — Intel Arc, Linux
    sycl_windows.py — Intel Arc, Windows  (future)
    cuda_linux.py   — NVIDIA, Linux       (future)
    cuda_windows.py — NVIDIA, Windows     (future)
    rocm_linux.py   — AMD, Linux          (future)
    cpu.py          — CPU fallback, all   (future)

The dispatcher in plugin.py detects the platform and GPU vendor, selects
the right backend, and delegates everything hardware-specific to it.
Shared logic (git, backup, build invocation) lives in shared.py.

Adding a new backend:
    1. Create backends/<name>.py subclassing LlamaCppBackend
    2. Implement all abstract methods
    3. Register it in plugin.py's _select_backend()
    4. Done — no other files need to change
"""

from __future__ import annotations

import abc
from pathlib import Path
from typing import Any

from aurini.core.base import CheckResult, RemedyResult


class LlamaCppBackend(abc.ABC):
    """
    Abstract base class for a llama.cpp build backend.

    A backend is responsible for everything that varies by platform and GPU:
    - Which pre-flight checks to run
    - Which cmake flags to use
    - How to source the GPU toolkit environment
    - How to construct the launch command environment
    - Which remedies apply

    Everything that is the same across backends (git clone/pull, backup,
    cmake invocation, binary verification) lives in shared.py and is called
    by the backend rather than reimplemented.
    """

    # ── Identity ───────────────────────────────────────────────────────────────

    @property
    @abc.abstractmethod
    def backend_id(self) -> str:
        """
        Stable machine-readable identifier for this backend.
        Example: "sycl_linux", "cuda_windows"
        """

    @property
    @abc.abstractmethod
    def display_name(self) -> str:
        """
        Human-readable name shown in the GUI alongside the plugin name.
        Example: "Intel Arc (SYCL) — Linux"
        """

    # ── Pre-flight checks ──────────────────────────────────────────────────────

    @abc.abstractmethod
    def get_checks(self) -> list[str]:
        """
        Return the ordered list of check IDs for this backend.

        Includes both backend-specific checks (toolkit present, GPU visible)
        and common checks (git, cmake, disk space, network). Order matters —
        declare toolkit checks before GPU visibility checks.
        """

    @abc.abstractmethod
    def run_check(self, check_id: str) -> CheckResult:
        """
        Run a single check. Must return CheckResult regardless of outcome.
        Never raises.
        """

    # ── Remedies ───────────────────────────────────────────────────────────────

    @abc.abstractmethod
    def run_remedy(self, remedy_id: str) -> RemedyResult:
        """
        Attempt a remedy. Must return RemedyResult regardless of outcome.
        Never raises.
        """

    # ── Build ──────────────────────────────────────────────────────────────────

    @abc.abstractmethod
    def cmake_flags(self, build_config: dict[str, Any]) -> list[str]:
        """
        Return the full list of cmake flags for this backend.

        build_config contains resolved build-phase settings (e.g. fp16: True).
        The backend constructs the complete flag list including any
        hardware-specific flags, compilers, and user-chosen options.

        Called by shared.run_build() — do not invoke cmake directly here.
        """

    @abc.abstractmethod
    def env_setup_script(self) -> str | None:
        """
        Return a shell command to source before running cmake and the binary.

        For oneAPI:  "source /opt/intel/oneapi/setvars.sh --force intel64"
        For CUDA:    None  (no sourcing needed, PATH is sufficient)
        For ROCm:    None  (typically)

        Returns None if no environment setup is needed.
        Used by shared.run_build() and build_launch_env().
        """

    # ── Launch ─────────────────────────────────────────────────────────────────

    @abc.abstractmethod
    def build_launch_env(self, base_env: dict[str, str]) -> dict[str, str]:
        """
        Return the environment dict to use when launching llama-server.

        Receives the current process environment as base_env. The backend
        adds any hardware-specific variables (e.g. ONEAPI_DEVICE_SELECTOR,
        CUDA_VISIBLE_DEVICES) and returns the complete env dict.

        The core passes this to subprocess when launching the process.
        """

    @abc.abstractmethod
    def binary_path(self, install_path: Path) -> Path:
        """
        Return the path to the llama-server binary for this backend.

        Most backends use the same path, but this allows for backend-specific
        build output locations if needed.
        """
