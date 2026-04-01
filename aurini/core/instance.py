"""
aurini.core.instance
~~~~~~~~~~~~~~~~~~~~
Instance management for AURINI.

A plugin is a template. An instance is a concrete installation of a plugin —
a specific build of llama.cpp at a specific path with a specific build config.

Each instance has:
- A stable machine-readable instance_id (e.g. "llama-cpp-arc")
- A user-given display_name (e.g. "llama.cpp — Arc")
- An install path, per OS (managed by AURINI or set by the user)
- Build-phase settings (compile flags chosen at install time)
- A reference to the active profile (launch-phase settings)
- An AURINI metadata directory at ~/aurini/instances/<instance_id>/

Profiles are stored separately in the metadata directory — see profile.py.
The instance record stores only a reference to the active profile_id.

File layout on disk:

    ~/aurini/
        instances/
            <instance_id>/
                instance.json        ← the instance record (this module)
                profiles/
                    <profile_id>.json    ← one file per profile (profile.py)

Usage:

    from aurini.core.instance import Instance, PathMode

    # Create a new instance (managed install path)
    inst = Instance.create(
        plugin_id="llama-cpp",
        display_name="llama.cpp — Arc",
        build_settings={
            "fp16": {"enabled": True, "value": True},
        },
    )

    # Create a new instance (user-chosen path)
    inst = Instance.create(
        plugin_id="llama-cpp",
        display_name="llama.cpp — Arc",
        build_settings={},
        path_mode=PathMode.CUSTOM,
        custom_paths={"linux": "~/llama.cpp", "windows": "C:/llama.cpp"},
    )

    # Load an existing instance
    inst = Instance.load("llama-cpp-arc")

    # List all instances
    instances = Instance.list_all()

    # Resolve install path for current OS
    path = inst.resolve_install_path()

    # Update a setting
    inst.set_build_setting("fp16", enabled=True, value=False)
    inst.save()
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from aurini.core.paths import (
    aurini_instances_dir,
    configured_os_keys,
    current_os,
    instance_metadata_dir,
    make_path_record,
    missing_os_keys,
    resolve_path,
    resolve_path_strict,
    set_path,
)


# ── Path mode ──────────────────────────────────────────────────────────────────

class PathMode(str, Enum):
    """
    How the install path for this instance is managed.

    MANAGED — AURINI picks the path: ~/aurini/instances/<id>/install/
              Simple, safe, no configuration needed. Default for new installs.

    CUSTOM  — User specifies the path. Supports existing installations and
              cases where the user wants the component at a specific location.
              AURINI's metadata always lives in the managed path regardless.
    """
    MANAGED = "managed"
    CUSTOM  = "custom"


# ── Instance ───────────────────────────────────────────────────────────────────

@dataclass
class Instance:
    """
    A concrete installation of an AURINI plugin.

    Do not construct directly — use Instance.create() for new instances or
    Instance.load() for existing ones. Both guarantee the record is on disk.

    Mutation methods (set_build_setting, set_active_profile, etc.) update the
    in-memory record only. Call save() to persist. This is intentional — the
    caller batches changes and saves once rather than flushing on every field.
    Exception: create() saves immediately as a side effect of construction.
    """

    instance_id:   str
    plugin_id:     str
    display_name:  str
    created:       str
    path_mode:     PathMode

    # Per-OS install paths. ~ is preserved in stored strings — expanded at resolve time.
    # For MANAGED instances, these are set by AURINI and the user cannot change them.
    # For CUSTOM instances, these are set by the user.
    paths: dict[str, str | None]

    # Per-OS paths to AURINI's metadata directory for this instance.
    # Always managed by AURINI — never user-set.
    aurini_managed_paths: dict[str, str | None]

    # Build-phase settings: {setting_id: {"enabled": bool, "value": <any>}}
    # These are the compile-time flags chosen at install time.
    # Changing them requires a rebuild — the GUI warns the user before allowing it.
    build_settings: dict[str, dict[str, Any]]

    # The profile_id of the active profile. May be None if no profiles exist yet.
    active_profile: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.path_mode, str):
            self.path_mode = PathMode(self.path_mode)

    # ── Factories ──────────────────────────────────────────────────────────────

    @classmethod
    def create(
        cls,
        plugin_id:      str,
        display_name:   str,
        build_settings: dict[str, dict[str, Any]],
        path_mode:      PathMode | str = PathMode.MANAGED,
        custom_paths:   dict[str, str | None] | None = None,
        instances_dir:  Path | None = None,
    ) -> Instance:
        """
        Create a new instance, write it to disk, and return it.

        For MANAGED instances, the install path is set automatically.
        For CUSTOM instances, provide custom_paths with per-OS path strings.

        instances_dir overrides the default ~/aurini/instances/ — useful for
        testing without touching the real AURINI data directory.

        Raises ValueError if a CUSTOM instance is created without custom_paths,
        or if the generated instance_id would collide with an existing one.
        """
        path_mode = PathMode(path_mode) if isinstance(path_mode, str) else path_mode

        if path_mode == PathMode.CUSTOM and not custom_paths:
            raise ValueError(
                "PathMode.CUSTOM requires custom_paths. "
                "Provide a dict with per-OS path strings, e.g. {'linux': '~/llama.cpp'}."
            )

        instances_dir = instances_dir or aurini_instances_dir()
        instance_id   = _generate_instance_id(plugin_id, display_name, instances_dir)
        created       = _now()

        meta_dir = instances_dir / instance_id
        managed_paths = make_path_record(
            linux=str(meta_dir / "install"),
            windows=str(meta_dir / "install"),
        )

        if path_mode == PathMode.MANAGED:
            install_paths = managed_paths
        else:
            install_paths = make_path_record(
                linux=custom_paths.get("linux"),
                windows=custom_paths.get("windows"),
                macos=custom_paths.get("macos"),
            )

        inst = cls(
            instance_id=instance_id,
            plugin_id=plugin_id,
            display_name=display_name,
            created=created,
            path_mode=path_mode,
            paths=install_paths,
            aurini_managed_paths=managed_paths,
            build_settings=build_settings,
        )

        inst._instances_dir = instances_dir
        inst.save()
        return inst

    @classmethod
    def load(
        cls,
        instance_id:   str,
        instances_dir: Path | None = None,
    ) -> Instance:
        """
        Load an existing instance from disk by instance_id.

        Raises FileNotFoundError if the instance does not exist.
        """
        instances_dir = instances_dir or aurini_instances_dir()
        path = instances_dir / instance_id / "instance.json"
        if not path.exists():
            raise FileNotFoundError(
                f"Instance '{instance_id}' not found at {path}.\n"
                "Check the instance_id or run Instance.list_all() to see available instances."
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        inst = cls.from_dict(data)
        inst._instances_dir = instances_dir
        return inst

    @classmethod
    def list_all(
        cls,
        instances_dir: Path | None = None,
        plugin_id:     str | None = None,
    ) -> list[Instance]:
        """
        Return all instances on disk, optionally filtered by plugin_id.

        Returns an empty list if the instances directory does not exist yet.
        Instances are returned in creation order (oldest first).
        """
        instances_dir = instances_dir or aurini_instances_dir()
        if not instances_dir.exists():
            return []

        results = []
        for candidate in sorted(instances_dir.iterdir()):
            record = candidate / "instance.json"
            if not record.exists():
                continue
            try:
                inst = cls.load(candidate.name, instances_dir=instances_dir)
                if plugin_id is None or inst.plugin_id == plugin_id:
                    results.append(inst)
            except Exception:
                # Corrupt or unreadable record — skip silently.
                # The GUI can surface a warning separately if needed.
                continue

        return results

    @classmethod
    def from_dict(cls, data: dict) -> Instance:
        """Deserialise an instance from a plain dict (e.g. loaded from JSON)."""
        return cls(
            instance_id=data["instance_id"],
            plugin_id=data["plugin_id"],
            display_name=data["display_name"],
            created=data["created"],
            path_mode=PathMode(data["path_mode"]),
            paths=data["paths"],
            aurini_managed_paths=data["aurini_managed_paths"],
            build_settings=data.get("build_settings", {}),
            active_profile=data.get("active_profile"),
        )

    # ── Serialisation ──────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "instance_id":         self.instance_id,
            "plugin_id":           self.plugin_id,
            "display_name":        self.display_name,
            "created":             self.created,
            "path_mode":           self.path_mode.value,
            "paths":               self.paths,
            "aurini_managed_paths": self.aurini_managed_paths,
            "build_settings":      self.build_settings,
            "active_profile":      self.active_profile,
        }

    def save(self) -> None:
        """
        Persist the current state of this instance to disk.

        Creates the instance metadata directory if it does not exist.
        Writes atomically — a crash mid-write leaves the previous file intact.
        """
        instances_dir = getattr(self, "_instances_dir", None) or aurini_instances_dir()
        meta_dir = instances_dir / self.instance_id
        meta_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(meta_dir / "instance.json", self.to_dict())

    def delete(self) -> None:
        """
        Remove this instance's metadata directory from disk.

        This removes the instance record and all associated profiles. It does
        NOT remove the installed component itself (the install path). Removing
        the component is done via the plugin's uninstall() method before calling
        this, so the action log captures what was done and how to undo it.

        Raises FileNotFoundError if the metadata directory does not exist.
        """
        instances_dir = getattr(self, "_instances_dir", None) or aurini_instances_dir()
        meta_dir = instances_dir / self.instance_id
        if not meta_dir.exists():
            raise FileNotFoundError(
                f"Metadata directory for instance '{self.instance_id}' not found at {meta_dir}."
            )
        import shutil
        shutil.rmtree(meta_dir)

    # ── Path resolution ────────────────────────────────────────────────────────

    def resolve_install_path(self, strict: bool = False) -> Path | None:
        """
        Resolve the install path for the current OS.

        strict=False (default): returns None if not configured — caller handles it.
        strict=True: raises RuntimeError if not configured, with a clear message.

        Use strict=True when about to do something that requires the path to be set
        (e.g. running a check against the install directory).
        """
        if strict:
            return resolve_path_strict(self.paths, instance_id=self.instance_id)
        return resolve_path(self.paths)

    def resolve_metadata_dir(self) -> Path:
        """
        Return the AURINI metadata directory for this instance on the current OS.

        Always set for managed instances. For custom instances this is still
        in ~/aurini/instances/<id>/ — the install path is separate from metadata.
        """
        return resolve_path_strict(self.aurini_managed_paths, instance_id=self.instance_id)

    def resolve_profiles_dir(self) -> Path:
        """Return the directory where this instance's profile files live."""
        return self.resolve_metadata_dir() / "profiles"

    def configured_platforms(self) -> list:
        """Return the OS keys that have an install path configured."""
        return configured_os_keys(self.paths)

    def missing_platforms(self) -> list:
        """Return the OS keys that have no install path configured."""
        return missing_os_keys(self.paths)

    def is_configured_for_current_os(self) -> bool:
        """True if an install path is configured for the current OS."""
        return resolve_path(self.paths) is not None

    # ── Build settings ─────────────────────────────────────────────────────────

    def get_build_setting(self, setting_id: str) -> dict[str, Any] | None:
        """
        Return the stored build setting for setting_id, or None if not set.

        Format: {"enabled": bool, "value": <any>}
        """
        return self.build_settings.get(setting_id)

    def set_build_setting(
        self,
        setting_id: str,
        enabled:    bool,
        value:      Any,
    ) -> None:
        """
        Update a build-phase setting in memory.

        Call save() after making all changes. The GUI should warn the user that
        changing build settings requires a rebuild before calling this.
        """
        self.build_settings[setting_id] = {"enabled": enabled, "value": value}

    def get_enabled_build_settings(self) -> dict[str, Any]:
        """
        Return only the enabled build settings as a flat {setting_id: value} dict.

        Convenient for passing to the plugin's install() / update() as the config
        argument — the plugin receives only what it needs to act on.
        """
        return {
            sid: entry["value"]
            for sid, entry in self.build_settings.items()
            if entry.get("enabled", False)
        }

    # ── Active profile ─────────────────────────────────────────────────────────

    def set_active_profile(self, profile_id: str | None) -> None:
        """
        Set the active profile for this instance.

        Pass None to clear the active profile (e.g. when the active profile
        is deleted). Call save() to persist.
        """
        self.active_profile = profile_id

    # ── Display helpers ────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        return (
            f"Instance(id={self.instance_id!r}, plugin={self.plugin_id!r}, "
            f"name={self.display_name!r}, mode={self.path_mode.value})"
        )


# ── Internal helpers ───────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slugify(text: str) -> str:
    """
    Convert a display name to a filesystem-safe, lowercase slug.

    "llama.cpp — Arc" → "llama-cpp-arc"
    "My Model (RTX 4090)" → "my-model-rtx-4090"
    """
    text = text.lower()
    # Replace em-dash, en-dash, and common separators with hyphens
    text = re.sub(r"[\s\-–—/\\|]+", "-", text)
    # Strip anything that isn't alphanumeric or hyphen
    text = re.sub(r"[^a-z0-9\-]", "", text)
    # Collapse repeated hyphens and strip leading/trailing
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text or "instance"


def _generate_instance_id(
    plugin_id:     str,
    display_name:  str,
    instances_dir: Path,
) -> str:
    """
    Generate a stable, human-readable instance_id that does not collide with
    any existing instance in instances_dir.

    "llama-cpp" + "llama.cpp — Arc" → "llama-cpp-arc"
    If that exists: "llama-cpp-arc-2", "llama-cpp-arc-3", etc.

    The plugin_id prefix is included so instance IDs are self-describing even
    when listed outside the context of a plugin.
    """
    # Build the slug from the display name, then strip any leading portion that
    # duplicates the plugin_id to avoid IDs like "llama-cpp-llamacpp-arc".
    #
    # Strategy: slugify both, then remove the plugin slug prefix from the
    # display slug if present. We also try stripping each hyphen-delimited
    # token of the plugin slug progressively so "llama-cpp" removes "llamacpp"
    # from "llamacpp-arc" correctly.
    slug = _slugify(display_name)
    plugin_slug = _slugify(plugin_id)

    # Build a set of prefixes to try removing (longest first)
    prefixes_to_strip = set()
    prefixes_to_strip.add(plugin_slug)
    # Also add the plugin_id itself slugified without internal hyphens
    prefixes_to_strip.add(plugin_slug.replace("-", ""))

    for prefix in sorted(prefixes_to_strip, key=len, reverse=True):
        if slug.startswith(prefix + "-"):
            slug = slug[len(prefix) + 1:]
            break
        elif slug == prefix:
            slug = ""
            break

    base = f"{plugin_id}-{slug}" if slug else plugin_id
    # Normalise: collapse double hyphens that may result from the above
    base = re.sub(r"-{2,}", "-", base).strip("-")

    candidate = base
    counter   = 2
    while (instances_dir / candidate).exists():
        candidate = f"{base}-{counter}"
        counter  += 1

    return candidate


def _atomic_write(path: Path, data: dict) -> None:
    """
    Write JSON to path atomically via temp file + rename.

    A crash mid-write leaves the previous file intact.
    """
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise
