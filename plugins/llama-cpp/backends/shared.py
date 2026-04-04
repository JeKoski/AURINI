"""
plugins/llama-cpp/backends/shared.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Shared utilities for llama.cpp backends.

Everything that is identical across all backends lives here:
- Running shell commands
- Git clone and pull
- File backup before updates
- cmake invocation
- Binary verification
- Repo identity check
- Git status parsing

Backends import from here rather than reimplementing. plugin.py also
imports from here for the uninstall flow.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any


# ── Shell execution ────────────────────────────────────────────────────────────

def run(
    command: list[str],
    cwd:     Path | None = None,
    timeout: int = 300,
) -> tuple[int, str]:
    """
    Run a command and return (returncode, combined stdout+stderr).

    Never raises — on exception returns (-1, traceback string).
    timeout defaults to 300s to accommodate apt installs and builds.
    """
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
        )
        combined = (result.stdout or "") + (result.stderr or "")
        return result.returncode, combined.strip()
    except Exception:
        return -1, traceback.format_exc()


def run_shell(
    script:  str,
    cwd:     Path | None = None,
    timeout: int = 300,
) -> tuple[int, str]:
    """
    Run a bash script string and return (returncode, combined output).

    Used for multi-step commands that require environment sourcing
    (e.g. source setvars.sh && cmake ...).
    Never raises.
    """
    try:
        result = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
        )
        combined = (result.stdout or "") + (result.stderr or "")
        return result.returncode, combined.strip()
    except Exception:
        return -1, traceback.format_exc()


def run_build_live(script: str, cwd: Path) -> int:
    """
    Run a build script with live output (not captured).

    Used for the cmake build step so the user sees progress in real time.
    Returns the exit code. Output is not captured — it streams to the
    terminal (or the GUI's log pane once that exists).
    """
    try:
        result = subprocess.run(
            ["bash", "-c", script],
            cwd=str(cwd),
        )
        return result.returncode
    except Exception:
        return -1


# ── Git operations ─────────────────────────────────────────────────────────────

LLAMA_CPP_REPO = "https://github.com/ggerganov/llama.cpp.git"


def clone(install_path: Path) -> tuple[bool, str]:
    """
    Clone llama.cpp from GitHub into install_path.

    Returns (success, raw_output).
    """
    code, raw = run(["git", "clone", LLAMA_CPP_REPO, str(install_path)])
    return code == 0, raw


def pull(repo: Path) -> tuple[bool, str]:
    """
    Reset any local modifications and pull the latest changes.

    Modified tracked files are reset with git checkout + clean so the pull
    proceeds cleanly. Backup must be done before calling this — files are
    expected to be safe before reset is called.

    Returns (success, raw_output).
    """
    # Discard local changes to tracked files so git pull can proceed
    run(["git", "checkout", "."], cwd=repo)
    run(["git", "clean", "-fd"],  cwd=repo)

    code, raw = run(["git", "pull"], cwd=repo)
    return code == 0, raw


def get_modified_files(repo: Path) -> tuple[list[str], list[str]]:
    """
    Return (untracked_files, modified_tracked_files) from git status --porcelain.

    Both types must be backed up before any git operation:
    - untracked:         new files git doesn't know about
    - modified tracked:  repo files with local changes that block git pull

    This distinction was a real bug in the original update_llama.py — only
    backing up untracked files caused failures when modified repo files
    (e.g. examples/sycl/build.sh) blocked git pull.
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo),
            capture_output=True,
            text=True,
        )
        untracked = []
        modified  = []
        for line in result.stdout.splitlines():
            xy    = line[:2]
            fpath = line[3:].strip().strip('"')
            if xy == "??":
                untracked.append(fpath)
            elif xy.strip() in ("M", "A", "AM", "MM"):
                modified.append(fpath)
        return untracked, modified
    except Exception:
        return [], []


def is_llama_repo(path: Path) -> bool:
    """
    Return True if path looks like a llama.cpp repository.
    Requires both a .git directory and a CMakeLists.txt.
    """
    return (path / ".git").exists() and (path / "CMakeLists.txt").exists()


# ── Backup ─────────────────────────────────────────────────────────────────────

def backup_modified_files(repo: Path) -> tuple[Path | None, int, int]:
    """
    Back up untracked and locally modified tracked files before a pull.

    Creates a timestamped backup directory in the user's home folder.
    Returns (backup_dir, untracked_count, modified_count).
    backup_dir is None if there was nothing to back up.

    Timestamp format: llama.cpp_backup_2026-04-01_14-32-07
    Using a timestamp (not just a date) prevents a second run on the same
    day from silently overwriting the first backup.
    """
    untracked, modified = get_modified_files(repo)
    all_files = untracked + modified
    if not all_files:
        return None, 0, 0

    timestamp  = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    backup_dir = Path.home() / f"llama.cpp_backup_{timestamp}"
    backup_dir.mkdir(parents=True, exist_ok=True)

    for rel in all_files:
        src = repo / rel
        dst = backup_dir / rel
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dst))

    return backup_dir, len(untracked), len(modified)


# ── Build ──────────────────────────────────────────────────────────────────────

def run_build(
    repo:          Path,
    cmake_flags:   list[str],
    cores:         int,
    env_setup:     str | None = None,
) -> tuple[bool, str]:
    """
    Run cmake configure + build inside repo/build/.

    cmake_flags: complete list of -D flags from the backend.
    cores:       number of parallel jobs.
    env_setup:   optional shell command to source before cmake
                 (e.g. "source /opt/intel/oneapi/setvars.sh --force intel64").

    Build output streams live to the terminal. Returns (success, note) where
    note is a short summary string (not the full output — that's live).
    """
    build_dir = repo / "build"
    build_dir.mkdir(exist_ok=True)

    flags_str  = " ".join(cmake_flags)
    repo_q     = shlex.quote(str(repo))
    setup_line = f"{env_setup}\n" if env_setup else ""

    script = (
        f"set -e\n"
        f"{setup_line}"
        f"cd {repo_q}\n"
        f"cmake -B build {flags_str}\n"
        f"cmake --build build --config Release -j{cores}\n"
    )

    code = run_build_live(script, cwd=repo)
    success = code == 0
    note = f"(build output displayed live — exit code: {code})"
    return success, note


def run_build_windows(
    repo:        Path,
    cmake_flags: list[str],
    cores:       int,
    setvars_bat: Path,
    extra_paths: list[Path] | None = None,
) -> tuple[bool, str]:
    """
    Run cmake configure + build on Windows inside the setvars.bat environment.

    Writes a temporary batch file that calls setvars.bat (which internally
    calls vcvarsall to set up cl.exe and the Windows SDK) and then runs cmake.
    Using a batch file rather than cmd.exe && chaining is critical: batch file
    `call` semantics propagate environment changes (including those made by
    setvars.bat's internal vcvars call) to subsequent commands in the same
    process. The && operator does NOT do this — each step runs in a fresh
    child environment, so cl.exe and kernel32.lib end up missing from cmake.

    setvars_bat:  full path to Intel oneAPI setvars.bat.
    extra_paths:  additional directories to prepend to PATH before cmake runs
                  (e.g. VS-internal cmake/bin and ninja dirs not on system PATH).

    Build output streams live to the terminal. Returns (success, note).
    """
    import tempfile

    build_dir = repo / "build"
    build_dir.mkdir(exist_ok=True)

    flags_str = " ".join(cmake_flags)

    # Prepend extra tool dirs (cmake, ninja) to PATH if needed.
    path_line = ""
    if extra_paths:
        joined = ";".join(str(p) for p in extra_paths if p)
        path_line = f'set "PATH={joined};%PATH%"\r\n'

    # Write a temp batch file. `call` propagates env changes from setvars.bat
    # (including its internal vcvars call) to the cmake steps that follow.
    bat_content = (
        f'@echo off\r\n'
        f'call "{setvars_bat}" intel64 --force\r\n'
        f'{path_line}'
        f'cmake -B build {flags_str}\r\n'
        f'if errorlevel 1 exit /b 1\r\n'
        f'cmake --build build --config Release -j{cores}\r\n'
        f'if errorlevel 1 exit /b 1\r\n'
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
            cwd=str(repo),
        )
        code = result.returncode
    except Exception:
        return False, traceback.format_exc()
    finally:
        try:
            Path(bat_path).unlink()
        except Exception:
            pass

    success = code == 0
    note = f"(build output displayed live — exit code: {code})"
    return success, note


def verify_binary(install_path: Path, binary_relative: str) -> bool:
    """
    Return True if the expected binary exists after a build.

    binary_relative: path relative to install_path, e.g. "build/bin/llama-server"
    """
    return (install_path / binary_relative).exists()


# ── Default build cores ────────────────────────────────────────────────────────

def default_build_cores() -> int:
    """
    Return a safe default number of parallel build jobs.

    Half of available CPU threads — balances build speed against keeping
    the system responsive during a long compile.
    """
    import os
    try:
        return max(1, (os.cpu_count() or 4) // 2)
    except Exception:
        return 4
