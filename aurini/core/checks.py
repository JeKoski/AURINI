"""
aurini.core.checks
~~~~~~~~~~~~~~~~~~
Core check library for AURINI plugins.

Each function in this module performs one specific check and returns a
CheckResult. Plugins call these from their run_check() implementation.

All checks are read-only — they observe the system, they never modify it.
All checks capture full raw output, even on unexpected failure.
All checks return a CheckResult regardless of outcome — they never raise.

Usage in a plugin's run_check():

    from aurini.core.checks import file_exists, command_output_contains

    def run_check(self, check_id: str) -> CheckResult:
        if check_id == "oneapi_present":
            return file_exists(
                check_id=check_id,
                path="/opt/intel/oneapi/setvars.sh",
                message_found="Intel oneAPI found at /opt/intel/oneapi/setvars.sh",
                message_missing="Intel oneAPI was not found. It is required for Intel Arc GPU support.",
                remedy_id="remedy_install_oneapi",
                risk="manual",
            )
        if check_id == "gpu_visible":
            return command_output_contains(
                check_id=check_id,
                command=["sycl-ls"],
                expected="level_zero:gpu",
                message_found="Intel Arc GPU detected via level_zero.",
                message_missing="No Intel Arc GPU was detected. Check drivers and group membership.",
                remedy_id="remedy_gpu_not_visible",
                risk="manual",
            )
        ...
"""

from __future__ import annotations

try:
    import grp
except ImportError:
    grp = None  # Windows — grp module is Unix-only
import importlib.util
import os
import platform
import shutil
import socket
import subprocess
import traceback
from pathlib import Path
from typing import Any

from aurini.core.base import CheckResult


# ── Internal helpers ───────────────────────────────────────────────────────────

def _run(command: list[str], env: dict | None = None, timeout: int = 30) -> tuple[int, str]:
    """
    Run a command and return (returncode, combined stdout+stderr).
    Never raises — on exception returns (-1, traceback string).
    """
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        combined = (result.stdout or "") + (result.stderr or "")
        return result.returncode, combined.strip()
    except Exception:
        return -1, traceback.format_exc()


def _ok(check_id: str, message: str, raw_output: str = "", metadata: dict | None = None) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        passed=True,
        message=message,
        raw_output=raw_output,
        metadata=metadata or {},
    )


def _fail(
    check_id: str,
    message: str,
    raw_output: str = "",
    remedy_id: str | None = None,
    risk: str | None = None,
    metadata: dict | None = None,
) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        passed=False,
        message=message,
        raw_output=raw_output,
        remedy_id=remedy_id,
        risk=risk,
        metadata=metadata or {},
    )


# ── System identity ────────────────────────────────────────────────────────────

def os_is(
    check_id: str,
    expected: str,
    remedy_id: str | None = None,
    risk: str | None = None,
) -> CheckResult:
    """
    Check the current OS type.

    expected: "linux" | "windows" | "macos"
    """
    mapping = {"linux": "Linux", "windows": "Windows", "macos": "Darwin"}
    current = platform.system()
    expected_sys = mapping.get(expected.lower(), expected)

    if current == expected_sys:
        return _ok(check_id, f"Operating system: {current}")
    return _fail(
        check_id,
        f"This plugin requires {expected}, but the current OS is {current}.",
        remedy_id=remedy_id,
        risk=risk,
    )


def arch_is(
    check_id: str,
    expected: str,
    remedy_id: str | None = None,
    risk: str | None = None,
) -> CheckResult:
    """
    Check the CPU architecture.

    expected: "x86_64" | "arm64" | "aarch64"
    """
    current = platform.machine()
    # Normalise arm64/aarch64 — they are the same architecture
    normalise = {"aarch64": "arm64"}
    current_norm = normalise.get(current.lower(), current.lower())
    expected_norm = normalise.get(expected.lower(), expected.lower())

    if current_norm == expected_norm:
        return _ok(check_id, f"CPU architecture: {current}")
    return _fail(
        check_id,
        f"This plugin requires {expected} architecture, but the current CPU is {current}.",
        remedy_id=remedy_id,
        risk=risk,
    )


# ── Hardware ───────────────────────────────────────────────────────────────────

def gpu_vendor_is(
    check_id: str,
    expected: str,
    remedy_id: str | None = None,
    risk: str | None = None,
) -> CheckResult:
    """
    Check the GPU vendor using platform-appropriate tools.

    expected: "intel" | "nvidia" | "amd" | "apple"

    Detection methods:
    - Linux:   lspci output
    - Windows: wmic path win32_VideoController
    - macOS:   system_profiler SPDisplaysDataType
    """
    system = platform.system()
    vendor_keywords = {
        "intel":  ["intel"],
        "nvidia": ["nvidia"],
        "amd":    ["amd", "radeon", "advanced micro devices"],
        "apple":  ["apple"],
    }
    keywords = vendor_keywords.get(expected.lower(), [expected.lower()])

    if system == "Linux":
        code, raw = _run(["lspci"])
        source = "lspci"
    elif system == "Windows":
        code, raw = _run(["wmic", "path", "win32_VideoController", "get", "name"])
        source = "wmic"
    elif system == "Darwin":
        code, raw = _run(["system_profiler", "SPDisplaysDataType"])
        source = "system_profiler"
    else:
        return _fail(check_id, f"GPU detection not supported on {system}.", raw_output="")

    raw_lower = raw.lower()
    if any(kw in raw_lower for kw in keywords):
        return _ok(check_id, f"{expected.title()} GPU detected via {source}.", raw_output=raw)
    return _fail(
        check_id,
        f"No {expected.title()} GPU was detected via {source}.",
        raw_output=raw,
        remedy_id=remedy_id,
        risk=risk,
    )


def gpu_visible(
    check_id: str,
    command: list[str],
    expected: str,
    message_found: str,
    message_missing: str,
    remedy_id: str | None = None,
    risk: str | None = None,
    env_setup_command: str | None = None,
    timeout: int = 30,
) -> CheckResult:
    """
    Check that a GPU is visible via a toolkit command (sycl-ls, nvidia-smi, rocm-smi).

    Checks that the command output contains expected — not just that the command
    exits 0. A command can exit successfully while not returning what we expect.

    env_setup_command: optional shell command to source before running the check
    (e.g. "source /opt/intel/oneapi/setvars.sh --force intel64 2>/dev/null").
    When provided, the check runs inside bash.
    """
    if env_setup_command:
        cmd_str = " ".join(command)
        script = f"{env_setup_command} && {cmd_str}"
        code, raw = _run(["bash", "-c", script], timeout=timeout)
    else:
        code, raw = _run(command, timeout=timeout)

    if expected.lower() in raw.lower():
        return _ok(check_id, message_found, raw_output=raw)
    return _fail(check_id, message_missing, raw_output=raw, remedy_id=remedy_id, risk=risk)


# ── User permissions ───────────────────────────────────────────────────────────

def user_in_group(
    check_id: str,
    group: str,
    remedy_id: str | None = None,
    risk: str | None = None,
) -> CheckResult:
    """
    Check that the current user is a member of a system group.
    Unix/Linux only — not applicable on Windows.
    """
    if grp is None:
        return _fail(
            check_id,
            "Group membership checks are not supported on Windows.",
            raw_output="",
        )
    try:
        username = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
        group_members = grp.getgrnam(group).gr_mem
        # Also check the user's primary group
        user_gid = os.getgid()
        group_gid = grp.getgrnam(group).gr_gid
        in_group = username in group_members or user_gid == group_gid

        if in_group:
            return _ok(check_id, f"User '{username}' is in the '{group}' group.")
        return _fail(
            check_id,
            f"User '{username}' is not in the '{group}' group. "
            f"This may prevent access to hardware at runtime.",
            remedy_id=remedy_id,
            risk=risk,
        )
    except KeyError:
        return _fail(
            check_id,
            f"Group '{group}' does not exist on this system.",
            remedy_id=remedy_id,
            risk=risk,
        )
    except Exception:
        return _fail(check_id, f"Could not check group membership for '{group}'.",
                     raw_output=traceback.format_exc())


def directory_writable(
    check_id: str,
    path: str | Path,
    remedy_id: str | None = None,
    risk: str | None = None,
) -> CheckResult:
    """
    Check that a directory exists and is writable by the current user.
    """
    p = Path(path).expanduser().resolve()
    if not p.exists():
        return _fail(check_id, f"Directory does not exist: {p}", remedy_id=remedy_id, risk=risk)
    if os.access(p, os.W_OK):
        return _ok(check_id, f"Directory is writable: {p}")
    return _fail(
        check_id,
        f"Directory exists but is not writable by the current user: {p}\n"
        f"If this was created by a previous run with sudo, fix it with:\n"
        f"  sudo chown -R $USER:$USER {p}",
        remedy_id=remedy_id,
        risk=risk,
    )


def directory_readable(
    check_id: str,
    path: str | Path,
    remedy_id: str | None = None,
    risk: str | None = None,
) -> CheckResult:
    """
    Check that a directory exists and is readable by the current user.
    """
    p = Path(path).expanduser().resolve()
    if not p.exists():
        return _fail(check_id, f"Directory does not exist: {p}", remedy_id=remedy_id, risk=risk)
    if os.access(p, os.R_OK):
        return _ok(check_id, f"Directory is readable: {p}")
    return _fail(
        check_id,
        f"Directory exists but is not readable by the current user: {p}",
        remedy_id=remedy_id,
        risk=risk,
    )


def can_run_without_sudo(
    check_id: str,
    command: list[str],
    remedy_id: str | None = None,
    risk: str | None = None,
) -> CheckResult:
    """
    Check that a command can be run without elevated privileges.
    Passes if the command exits with code 0 (ignores output content).
    """
    code, raw = _run(command)
    if code == 0:
        return _ok(check_id, f"Command runs without sudo: {' '.join(command)}", raw_output=raw)
    return _fail(
        check_id,
        f"Command failed or requires elevated privileges: {' '.join(command)}",
        raw_output=raw,
        remedy_id=remedy_id,
        risk=risk,
    )


# ── Software presence ──────────────────────────────────────────────────────────

def file_exists(
    check_id: str,
    path: str | Path,
    message_found: str | None = None,
    message_missing: str | None = None,
    remedy_id: str | None = None,
    risk: str | None = None,
) -> CheckResult:
    """
    Check that a file or directory exists at path.
    """
    p = Path(path).expanduser().resolve()
    if p.exists():
        return _ok(check_id, message_found or f"Found: {p}")
    return _fail(
        check_id,
        message_missing or f"Not found: {p}",
        remedy_id=remedy_id,
        risk=risk,
    )


def command_exists(
    check_id: str,
    command: str,
    remedy_id: str | None = None,
    risk: str | None = None,
) -> CheckResult:
    """
    Check that a command is available on PATH.
    """
    path = shutil.which(command)
    if path:
        return _ok(check_id, f"Found {command} at: {path}", metadata={"path": path})
    return _fail(
        check_id,
        f"'{command}' was not found. Please install it and try again.",
        remedy_id=remedy_id,
        risk=risk,
    )


def command_succeeds(
    check_id: str,
    command: list[str],
    message_ok: str | None = None,
    message_fail: str | None = None,
    remedy_id: str | None = None,
    risk: str | None = None,
    timeout: int = 30,
) -> CheckResult:
    """
    Check that a command exits with code 0.

    Prefer command_output_contains when you need to verify the output, not
    just that the command ran. A command can exit 0 while returning unexpected
    content (e.g. a version mismatch, a deprecation warning filling stdout).
    """
    code, raw = _run(command, timeout=timeout)
    cmd_str = " ".join(command)
    if code == 0:
        return _ok(check_id, message_ok or f"Command succeeded: {cmd_str}", raw_output=raw)
    return _fail(
        check_id,
        message_fail or f"Command failed (exit {code}): {cmd_str}",
        raw_output=raw,
        remedy_id=remedy_id,
        risk=risk,
    )


def command_output_contains(
    check_id: str,
    command: list[str],
    expected: str,
    message_found: str | None = None,
    message_missing: str | None = None,
    remedy_id: str | None = None,
    risk: str | None = None,
    timeout: int = 30,
) -> CheckResult:
    """
    Check that a command's output contains an expected string.

    Preferred over command_succeeds for tool detection — a command can exit 0
    while not returning what we expect.
    """
    code, raw = _run(command, timeout=timeout)
    cmd_str = " ".join(command)
    if expected.lower() in raw.lower():
        return _ok(
            check_id,
            message_found or f"'{expected}' found in output of: {cmd_str}",
            raw_output=raw,
        )
    return _fail(
        check_id,
        message_missing or f"'{expected}' was not found in output of: {cmd_str}",
        raw_output=raw,
        remedy_id=remedy_id,
        risk=risk,
    )


def version_gte(
    check_id: str,
    command: list[str],
    minimum: str,
    message_ok: str | None = None,
    message_fail: str | None = None,
    remedy_id: str | None = None,
    risk: str | None = None,
    timeout: int = 30,
) -> CheckResult:
    """
    Check that a tool's version meets a minimum requirement.

    Parses the first version-like string (digits and dots) found in the
    command's output. Works for most tools that follow semver or similar.

    minimum: version string, e.g. "3.10.0" or "2.39"
    """
    import re

    code, raw = _run(command, timeout=timeout)
    cmd_str = " ".join(command)

    match = re.search(r"(\d+(?:\.\d+)+)", raw)
    if not match:
        return _fail(
            check_id,
            f"Could not parse a version number from: {cmd_str}",
            raw_output=raw,
            remedy_id=remedy_id,
            risk=risk,
        )

    def parse(v: str) -> tuple[int, ...]:
        return tuple(int(x) for x in v.split("."))

    found_str = match.group(1)
    try:
        found = parse(found_str)
        minimum_t = parse(minimum)
    except ValueError:
        return _fail(
            check_id,
            f"Could not compare versions: found '{found_str}', minimum '{minimum}'.",
            raw_output=raw,
        )

    if found >= minimum_t:
        return _ok(
            check_id,
            message_ok or f"{cmd_str}: version {found_str} meets minimum {minimum}.",
            raw_output=raw,
            metadata={"version": found_str},
        )
    return _fail(
        check_id,
        message_fail or f"{cmd_str}: version {found_str} is below minimum {minimum}.",
        raw_output=raw,
        remedy_id=remedy_id,
        risk=risk,
        metadata={"version": found_str},
    )


def python_package_installed(
    check_id: str,
    package: str,
    python_executable: str = "python3",
    remedy_id: str | None = None,
    risk: str | None = None,
) -> CheckResult:
    """
    Check that a Python package is importable in a given Python environment.

    python_executable: path to the Python binary to check against.
    Defaults to "python3" (system Python). Pass a venv's python path to check
    a managed environment.
    """
    code, raw = _run([python_executable, "-c", f"import {package}"])
    if code == 0:
        return _ok(check_id, f"Python package '{package}' is available.", raw_output=raw)
    return _fail(
        check_id,
        f"Python package '{package}' is not installed in {python_executable}.",
        raw_output=raw,
        remedy_id=remedy_id,
        risk=risk,
    )


def process_running(
    check_id: str,
    process_name: str,
    remedy_id: str | None = None,
    risk: str | None = None,
) -> CheckResult:
    """
    Check whether a named process is currently running.

    Uses 'pgrep' on Linux/macOS and 'tasklist' on Windows.
    """
    system = platform.system()
    if system == "Windows":
        code, raw = _run(["tasklist", "/FI", f"IMAGENAME eq {process_name}"])
        running = process_name.lower() in raw.lower()
    else:
        code, raw = _run(["pgrep", "-x", process_name])
        running = code == 0

    if running:
        return _ok(check_id, f"Process is running: {process_name}", raw_output=raw)
    return _fail(
        check_id,
        f"Process is not running: {process_name}",
        raw_output=raw,
        remedy_id=remedy_id,
        risk=risk,
    )


# ── Network and disk ───────────────────────────────────────────────────────────

def internet_reachable(
    check_id: str,
    host: str = "8.8.8.8",
    port: int = 53,
    timeout: int = 5,
    remedy_id: str | None = None,
    risk: str | None = None,
) -> CheckResult:
    """
    Check basic internet connectivity by opening a socket to a known host.

    Defaults to Google's DNS (8.8.8.8:53) — reliable, fast, no HTTP overhead.
    """
    try:
        socket.setdefaulttimeout(timeout)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect((host, port))
        return _ok(check_id, "Internet connection is available.")
    except OSError:
        return _fail(
            check_id,
            "No internet connection detected. A connection is needed to download files.",
            remedy_id=remedy_id,
            risk=risk,
        )


def host_reachable(
    check_id: str,
    host: str,
    port: int = 443,
    timeout: int = 5,
    remedy_id: str | None = None,
    risk: str | None = None,
) -> CheckResult:
    """
    Check that a specific host is reachable on a given port.

    Useful for verifying download sources before starting a large download.
    """
    try:
        socket.setdefaulttimeout(timeout)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect((host, port))
        return _ok(check_id, f"Host is reachable: {host}:{port}")
    except OSError:
        return _fail(
            check_id,
            f"Could not reach {host}:{port}. Check your internet connection.",
            remedy_id=remedy_id,
            risk=risk,
        )


def disk_space_gte(
    check_id: str,
    path: str | Path,
    minimum_gb: float,
    remedy_id: str | None = None,
    risk: str | None = None,
) -> CheckResult:
    """
    Check that available disk space at path is >= minimum_gb gigabytes.

    If path does not exist yet (e.g. the install directory hasn't been created),
    walks up to the nearest existing ancestor and checks that instead. This is
    the normal case for a fresh install — the target directory won't exist until
    the clone step runs.

    Always check before large downloads or builds — insufficient disk space
    causes confusing mid-install failures that are hard to diagnose.
    """
    p = Path(path).expanduser().resolve()

    # Walk up to the nearest existing ancestor so statvfs has something to work with.
    # The disk usage of the volume that will contain `p` is what we care about.
    check_path = p
    while not check_path.exists():
        parent = check_path.parent
        if parent == check_path:
            # Reached filesystem root and nothing exists — shouldn't happen on a
            # sane system, but fall back gracefully rather than looping forever.
            break
        check_path = parent

    try:
        stat = shutil.disk_usage(check_path)
        available_gb = stat.free / (1024 ** 3)

        # Tell the user which path was actually checked if it differs from the
        # requested path — so they know what they're looking at.
        location_note = (
            f" (checked at {check_path}, nearest existing ancestor of {p})"
            if check_path != p
            else f" at {p}"
        )

        if available_gb >= minimum_gb:
            return _ok(
                check_id,
                f"Available disk space{location_note}: {available_gb:.1f} GB "
                f"(minimum required: {minimum_gb} GB).",
                metadata={"available_gb": round(available_gb, 2)},
            )
        return _fail(
            check_id,
            f"Not enough disk space{location_note}. "
            f"Available: {available_gb:.1f} GB, required: {minimum_gb} GB.",
            remedy_id=remedy_id,
            risk=risk,
            metadata={"available_gb": round(available_gb, 2)},
        )
    except Exception:
        return _fail(
            check_id,
            f"Could not check disk space at {check_path}.",
            raw_output=traceback.format_exc(),
        )


# ── Runtime state ──────────────────────────────────────────────────────────────

def port_in_use(
    check_id: str,
    port: int,
    host: str = "127.0.0.1",
    remedy_id: str | None = None,
    risk: str | None = None,
) -> CheckResult:
    """
    Check whether a port is currently bound on the local machine.

    Passes if the port IS in use. Use this to verify a service is running,
    or invert the logic in your plugin if you need to confirm a port is free.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            result = s.connect_ex((host, port))
            in_use = result == 0
        if in_use:
            return _ok(check_id, f"Port {port} is in use on {host}.")
        return _fail(
            check_id,
            f"Port {port} is not in use on {host}.",
            remedy_id=remedy_id,
            risk=risk,
        )
    except Exception:
        return _fail(
            check_id,
            f"Could not check port {port}.",
            raw_output=traceback.format_exc(),
        )


def lock_file_present(
    check_id: str,
    path: str | Path,
    remedy_id: str | None = None,
    risk: str | None = None,
) -> CheckResult:
    """
    Check for the presence of a lock file.

    Passes if the lock file IS present. Useful for detecting that another
    instance of a process is already running before attempting to launch.
    """
    p = Path(path).expanduser().resolve()
    if p.exists():
        return _ok(check_id, f"Lock file present: {p}")
    return _fail(
        check_id,
        f"Lock file not found: {p}",
        remedy_id=remedy_id,
        risk=risk,
    )
