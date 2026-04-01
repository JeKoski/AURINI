"""
aurini.core.paths
~~~~~~~~~~~~~~~~~
Cross-OS path resolution for AURINI instances.

Each instance stores paths per OS so the same instance record works correctly
whether the user is on Linux or Windows. AURINI resolves the right path for
the current OS at runtime and prompts the user to configure missing ones
rather than silently failing.

This also handles the common case of users who run from a shared drive across
OS installations — configure both paths once and AURINI just works on either.

Usage:

    from aurini.core.paths import resolve_path, current_os, OsKey

    # Resolve the install path for the current OS
    path = resolve_path(instance["paths"])

    # Check which OS we're on
    if current_os() == OsKey.LINUX:
        ...

    # Build a fresh per-OS path dict
    paths = make_path_record(linux="~/llama.cpp", windows="C:/llama.cpp")
"""

from __future__ import annotations

import platform
from enum import Enum
from pathlib import Path


# ── OS keys ───────────────────────────────────────────────────────────────────

class OsKey(str, Enum):
    """
    Canonical OS identifiers used as keys in per-OS path records.
    These match the values used in plugin.json 'platforms' arrays.
    """
    LINUX   = "linux"
    WINDOWS = "windows"
    MACOS   = "macos"


def current_os() -> OsKey:
    """Return the OsKey for the current platform."""
    system = platform.system()
    mapping = {
        "Linux":  OsKey.LINUX,
        "Windows": OsKey.WINDOWS,
        "Darwin": OsKey.MACOS,
    }
    try:
        return mapping[system]
    except KeyError:
        raise RuntimeError(
            f"Unsupported platform: {system}. "
            "AURINI supports Linux, Windows, and macOS."
        )


# ── Path records ───────────────────────────────────────────────────────────────

def make_path_record(
    linux:   str | Path | None = None,
    windows: str | Path | None = None,
    macos:   str | Path | None = None,
) -> dict[str, str | None]:
    """
    Build a per-OS path record suitable for storing in an instance dict.

    Values are stored as strings (not Path objects) so they serialise cleanly
    to JSON. ~ is preserved as-is — expansion happens at resolve time so it
    works correctly on whichever OS loads the record.

    Example:
        paths = make_path_record(
            linux="~/llama.cpp",
            windows="C:/llama.cpp",
        )
        # {"linux": "~/llama.cpp", "windows": "C:/llama.cpp", "macos": None}
    """
    return {
        OsKey.LINUX.value:   str(linux)   if linux   is not None else None,
        OsKey.WINDOWS.value: str(windows) if windows is not None else None,
        OsKey.MACOS.value:   str(macos)   if macos   is not None else None,
    }


def resolve_path(
    paths:  dict[str, str | None],
    os_key: OsKey | None = None,
) -> Path | None:
    """
    Resolve the path for the given OS from a per-OS path record.

    os_key defaults to the current OS. Returns None if no path is configured
    for the target OS — the caller should prompt the user to set one rather
    than failing silently.

    Expands ~ and resolves to an absolute path. Does not check whether the
    path exists — that is the caller's responsibility.

    Example:
        path = resolve_path(instance["paths"])
        if path is None:
            # Prompt user to configure the path for this OS
            ...
    """
    os_key = os_key or current_os()
    raw = paths.get(os_key.value)
    if raw is None:
        return None
    return Path(raw).expanduser().resolve()


def resolve_path_strict(
    paths:       dict[str, str | None],
    os_key:      OsKey | None = None,
    instance_id: str = "",
) -> Path:
    """
    Resolve the path for the given OS, raising if not configured.

    Use this when a path is required and the caller has already ensured
    it should be set. For optional paths, use resolve_path() instead.

    Raises RuntimeError with a clear message if the path is not configured,
    so the core can surface it to the user.
    """
    os_key = os_key or current_os()
    path   = resolve_path(paths, os_key)
    if path is None:
        instance_str = f" for instance '{instance_id}'" if instance_id else ""
        raise RuntimeError(
            f"No path configured for {os_key.value}{instance_str}.\n"
            "Open instance settings and set the path for this operating system."
        )
    return path


def set_path(
    paths:  dict[str, str | None],
    value:  str | Path,
    os_key: OsKey | None = None,
) -> dict[str, str | None]:
    """
    Return a new path record with the path for the given OS set to value.

    Does not mutate the input dict — returns a new one. ~ is preserved
    so the record stays portable across user accounts.

    Example:
        updated = set_path(instance["paths"], "~/llama.cpp")
    """
    os_key  = os_key or current_os()
    updated = dict(paths)
    updated[os_key.value] = str(value)
    return updated


def configured_os_keys(paths: dict[str, str | None]) -> list[OsKey]:
    """
    Return the list of OS keys that have a path configured (non-None).

    Useful for showing the user which platforms are set up for an instance.
    """
    return [OsKey(k) for k, v in paths.items() if v is not None]


def missing_os_keys(paths: dict[str, str | None]) -> list[OsKey]:
    """
    Return the list of OS keys that have no path configured.

    Useful for prompting the user to complete setup on additional platforms.
    """
    all_keys = {k.value for k in OsKey}
    return [OsKey(k) for k, v in paths.items() if v is None and k in all_keys]


def is_configured_for_current_os(paths: dict[str, str | None]) -> bool:
    """
    Return True if a path is configured for the current OS.

    Quick check before attempting to resolve — avoids the RuntimeError
    from resolve_path_strict when you just need a boolean.
    """
    return resolve_path(paths) is not None


# ── AURINI managed paths ───────────────────────────────────────────────────────

def aurini_data_dir() -> Path:
    """
    Return AURINI's data directory for the current OS.

    This is where AURINI stores its own metadata (instance records, profiles,
    action logs, managed Python runtime). It is separate from the install path
    of any component.

    Linux/macOS : ~/aurini/
    Windows     : %APPDATA%/aurini/   (typically C:/Users/<name>/AppData/Roaming/aurini/)
    """
    os_key = current_os()
    if os_key == OsKey.WINDOWS:
        import os
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "aurini"
        # Fallback if APPDATA is not set
        return Path.home() / "AppData" / "Roaming" / "aurini"
    return Path.home() / "aurini"


def aurini_logs_dir() -> Path:
    """Return the logs directory inside AURINI's data directory."""
    return aurini_data_dir() / "logs"


def aurini_instances_dir() -> Path:
    """
    Return the directory where AURINI stores per-instance metadata.

    Each instance gets a subdirectory here for its profiles, build config,
    and action log — regardless of where the component itself is installed.
    """
    return aurini_data_dir() / "instances"


def instance_metadata_dir(instance_id: str) -> Path:
    """
    Return the metadata directory for a specific instance.

    This always exists inside AURINI's managed data directory, even if the
    component itself is installed at a custom user-chosen path.
    """
    return aurini_instances_dir() / instance_id


def aurini_runtime_dir() -> Path:
    """
    Return the directory where AURINI's managed Python runtime lives.

    This is the AURINI-owned Python installation — separate from any system
    Python or user Python environments.
    """
    return aurini_data_dir() / "runtime" / "python"
