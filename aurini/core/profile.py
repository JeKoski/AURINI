"""
aurini.core.profile
~~~~~~~~~~~~~~~~~~~
Profile management for AURINI instances.

A profile is a named snapshot of all launch-phase settings for a plugin
instance. Switching profiles changes what gets passed to the process at
launch — a different model, different context size, different GPU layers.

The model path lives in the profile, not the instance. This is what makes
switching between Gemma and Qwen practical — the entire launch configuration
including the model is captured in the profile, and switching is instant.

Profiles are stored as individual JSON files in the instance's metadata
directory. Each profile is a separate file so they can be added, edited,
or deleted without rewriting the instance record.

File layout:

    ~/aurini/instances/<instance_id>/profiles/
        <profile_id>.json
        <profile_id>.json
        ...

Usage:

    from aurini.core.profile import Profile
    from aurini.core.instance import Instance

    inst = Instance.load("llama-cpp-arc")

    # Create a profile
    profile = Profile.create(
        instance=inst,
        display_name="Gemma 27B — High Quality",
        notes="Best quality, needs full 8GB VRAM free",
        settings={
            "model_path": {"enabled": True,  "value": "~/models/gemma-27b-q4.gguf"},
            "ctx_size":   {"enabled": True,  "value": 8192},
            "gpu_layers": {"enabled": True,  "value": 99},
            "flash_attn": {"enabled": True,  "value": True},
        },
        make_default=True,
    )

    # Load a profile
    profile = Profile.load(instance=inst, profile_id="gemma-27b-high-quality")

    # List all profiles for an instance
    profiles = Profile.list_all(instance=inst)

    # Get enabled settings as a flat dict for build_launch_command()
    launch_config = profile.get_enabled_settings()

    # Add a custom arg
    profile.add_custom_arg(flag="--threads", value="6")
    profile.save()

    # Delete a profile
    profile.delete()
    # If it was the active profile, clear it on the instance
    if inst.active_profile == profile.profile_id:
        inst.set_active_profile(None)
        inst.save()
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aurini.core.instance import Instance, _atomic_write, _now, _slugify


# ── Profile ────────────────────────────────────────────────────────────────────

@dataclass
class Profile:
    """
    A named launch-phase configuration for a plugin instance.

    Do not construct directly — use Profile.create() or Profile.load().

    Mutation methods update in-memory state only. Call save() to persist.
    """

    profile_id:   str
    display_name: str
    created:      str

    # Whether this is the instance's default profile.
    # Only one profile per instance should have is_default=True.
    # Use Profile.set_default() to change the default — it handles clearing
    # the flag on the previous default automatically.
    is_default: bool

    # Launch-phase settings: {setting_id: {"enabled": bool, "value": <any>}}
    # These are passed to the process at launch. Changing them takes effect
    # immediately on the next launch — no rebuild required.
    settings: dict[str, dict[str, Any]]

    # Free-form note shown in the GUI — useful for reminding the user what
    # this profile is for (e.g. "Best quality, needs full 8GB VRAM free").
    notes: str

    # Custom arguments: [{"flag": str, "value": str | None, "enabled": bool}]
    # Passed through as-is after the preset settings. AURINI does not validate
    # these — they are the user's responsibility.
    # value is None for bare flags (e.g. --verbose), a string otherwise.
    custom_args: list[dict[str, Any]]

    # Back-reference to the instance this profile belongs to.
    # Not serialised — set by load() / create() at runtime.
    _instance: Instance = field(default=None, repr=False, compare=False)

    # ── Factories ──────────────────────────────────────────────────────────────

    @classmethod
    def create(
        cls,
        instance:     Instance,
        display_name: str,
        settings:     dict[str, dict[str, Any]],
        notes:        str = "",
        custom_args:  list[dict[str, Any]] | None = None,
        make_default: bool = False,
    ) -> Profile:
        """
        Create a new profile for an instance, write it to disk, and return it.

        make_default=True sets this as the instance's active profile and clears
        the is_default flag on any existing default profile.

        Profile IDs are generated from the display_name and guaranteed unique
        within the instance.
        """
        profiles_dir = instance.resolve_profiles_dir()
        profiles_dir.mkdir(parents=True, exist_ok=True)

        profile_id = _generate_profile_id(display_name, profiles_dir)

        # If make_default, clear is_default on existing profiles first
        if make_default:
            _clear_default_flag(profiles_dir)

        profile = cls(
            profile_id=profile_id,
            display_name=display_name,
            created=_now(),
            is_default=make_default,
            settings=settings,
            notes=notes,
            custom_args=custom_args or [],
        )
        profile._instance = instance
        profile._profiles_dir = profiles_dir
        profile.save()

        if make_default:
            instance.set_active_profile(profile_id)
            instance.save()

        return profile

    @classmethod
    def load(
        cls,
        instance:   Instance,
        profile_id: str,
    ) -> Profile:
        """
        Load an existing profile by profile_id.

        Raises FileNotFoundError if the profile does not exist.
        """
        profiles_dir = instance.resolve_profiles_dir()
        path = profiles_dir / f"{profile_id}.json"
        if not path.exists():
            raise FileNotFoundError(
                f"Profile '{profile_id}' not found for instance '{instance.instance_id}'.\n"
                "Run Profile.list_all(instance) to see available profiles."
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        profile = cls.from_dict(data)
        profile._instance = instance
        profile._profiles_dir = profiles_dir
        return profile

    @classmethod
    def list_all(cls, instance: Instance) -> list[Profile]:
        """
        Return all profiles for an instance, sorted by creation time (oldest first).

        Returns an empty list if no profiles exist yet.
        """
        profiles_dir = instance.resolve_profiles_dir()
        if not profiles_dir.exists():
            return []

        results = []
        for path in sorted(profiles_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                profile = cls.from_dict(data)
                profile._instance = instance
                profile._profiles_dir = profiles_dir
                results.append(profile)
            except Exception:
                continue

        # Sort by creation timestamp
        results.sort(key=lambda p: p.created)
        return results

    @classmethod
    def from_dict(cls, data: dict) -> Profile:
        return cls(
            profile_id=data["profile_id"],
            display_name=data["display_name"],
            created=data["created"],
            is_default=data.get("is_default", False),
            settings=data.get("settings", {}),
            notes=data.get("notes", ""),
            custom_args=data.get("custom_args", []),
        )

    # ── Serialisation ──────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "profile_id":   self.profile_id,
            "display_name": self.display_name,
            "created":      self.created,
            "is_default":   self.is_default,
            "settings":     self.settings,
            "notes":        self.notes,
            "custom_args":  self.custom_args,
        }

    def save(self) -> None:
        """
        Persist the current state of this profile to disk.

        Creates the profiles directory if it does not exist.
        Writes atomically.
        """
        profiles_dir = getattr(self, "_profiles_dir", None)
        if profiles_dir is None:
            if self._instance is None:
                raise RuntimeError(
                    "Cannot save a Profile with no associated Instance. "
                    "Use Profile.create() or Profile.load() instead of constructing directly."
                )
            profiles_dir = self._instance.resolve_profiles_dir()
            profiles_dir.mkdir(parents=True, exist_ok=True)
            self._profiles_dir = profiles_dir

        _atomic_write(profiles_dir / f"{self.profile_id}.json", self.to_dict())

    def delete(self) -> None:
        """
        Delete this profile's file from disk.

        Does NOT automatically update the instance's active_profile — the
        caller is responsible for clearing or reassigning it if this was
        the active profile. See module docstring for the recommended pattern.
        """
        profiles_dir = getattr(self, "_profiles_dir", None)
        if profiles_dir is None and self._instance is not None:
            profiles_dir = self._instance.resolve_profiles_dir()

        if profiles_dir is None:
            raise RuntimeError("Cannot delete profile: profiles_dir is not set.")

        path = profiles_dir / f"{self.profile_id}.json"
        if path.exists():
            path.unlink()

    # ── Settings ───────────────────────────────────────────────────────────────

    def get_setting(self, setting_id: str) -> dict[str, Any] | None:
        """Return the stored setting for setting_id, or None if not set."""
        return self.settings.get(setting_id)

    def set_setting(self, setting_id: str, enabled: bool, value: Any) -> None:
        """
        Update a launch-phase setting in memory. Call save() to persist.

        Takes effect on the next launch — no rebuild required.
        """
        self.settings[setting_id] = {"enabled": enabled, "value": value}

    def remove_setting(self, setting_id: str) -> None:
        """Remove a setting entirely from this profile. Call save() to persist."""
        self.settings.pop(setting_id, None)

    def get_enabled_settings(self) -> dict[str, Any]:
        """
        Return enabled settings as a flat {setting_id: value} dict.

        This is what gets passed to the plugin's build_launch_command() as the
        profile argument — only enabled settings, values only (no metadata).
        """
        return {
            sid: entry["value"]
            for sid, entry in self.settings.items()
            if entry.get("enabled", False)
        }

    # ── Custom args ────────────────────────────────────────────────────────────

    def add_custom_arg(
        self,
        flag:    str,
        value:   str | None = None,
        enabled: bool = True,
    ) -> None:
        """
        Add a custom argument to this profile. Call save() to persist.

        flag   — the flag name, e.g. "--threads" or "--verbose"
        value  — the value, e.g. "6". None for bare flags.
        enabled — whether to include this arg at launch. Defaults to True.

        Custom args are passed through as-is after the preset settings.
        AURINI does not validate them.
        """
        self.custom_args.append({"flag": flag, "value": value, "enabled": enabled})

    def remove_custom_arg(self, index: int) -> None:
        """
        Remove a custom argument by its list index. Call save() to persist.

        Raises IndexError if the index is out of range.
        """
        if index < 0 or index >= len(self.custom_args):
            raise IndexError(
                f"Custom arg index {index} is out of range "
                f"(profile has {len(self.custom_args)} custom args)."
            )
        self.custom_args.pop(index)

    def set_custom_arg_enabled(self, index: int, enabled: bool) -> None:
        """
        Toggle a custom argument on or off without removing it. Call save() to persist.

        This is the recommended way to disable a custom arg — remove() loses
        the flag name, which makes it hard to re-enable later.
        """
        if index < 0 or index >= len(self.custom_args):
            raise IndexError(
                f"Custom arg index {index} is out of range "
                f"(profile has {len(self.custom_args)} custom args)."
            )
        self.custom_args[index]["enabled"] = enabled

    def get_enabled_custom_args(self) -> list[dict[str, Any]]:
        """Return only the enabled custom args."""
        return [arg for arg in self.custom_args if arg.get("enabled", True)]

    def build_custom_arg_tokens(self) -> list[str]:
        """
        Build the argv tokens for all enabled custom args.

        Returns a flat list of strings ready to append to a launch command.

        Example:
            [{"flag": "--threads", "value": "6", "enabled": True},
             {"flag": "--verbose", "value": None, "enabled": True}]
            →  ["--threads", "6", "--verbose"]
        """
        tokens = []
        for arg in self.get_enabled_custom_args():
            tokens.append(arg["flag"])
            if arg.get("value") is not None:
                tokens.append(str(arg["value"]))
        return tokens

    # ── Default management ─────────────────────────────────────────────────────

    def set_as_default(self) -> None:
        """
        Make this the default profile for its instance.

        Clears the is_default flag on all other profiles, sets it on this one,
        updates the instance's active_profile, and saves everything.

        Requires this profile to have been created via Profile.create() or
        Profile.load() (i.e. _instance must be set).
        """
        if self._instance is None:
            raise RuntimeError(
                "Cannot set default: profile has no associated instance. "
                "Use Profile.load(instance, ...) to load with an instance reference."
            )

        profiles_dir = getattr(self, "_profiles_dir", None) or self._instance.resolve_profiles_dir()
        _clear_default_flag(profiles_dir, exclude_id=self.profile_id)

        self.is_default = True
        self.save()

        self._instance.set_active_profile(self.profile_id)
        self._instance.save()

    # ── Display ────────────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        default_str = " [default]" if self.is_default else ""
        return f"Profile(id={self.profile_id!r}, name={self.display_name!r}{default_str})"


# ── Internal helpers ───────────────────────────────────────────────────────────

def _generate_profile_id(display_name: str, profiles_dir: Path) -> str:
    """
    Generate a stable, human-readable profile_id that does not collide with
    existing profiles in profiles_dir.

    "Gemma 27B — High Quality" → "gemma-27b-high-quality"
    If that exists: "gemma-27b-high-quality-2", etc.
    """
    base = _slugify(display_name) or "profile"

    candidate = base
    counter   = 2
    while (profiles_dir / f"{candidate}.json").exists():
        candidate = f"{base}-{counter}"
        counter  += 1

    return candidate


def _clear_default_flag(profiles_dir: Path, exclude_id: str | None = None) -> None:
    """
    Clear the is_default flag on all profiles in profiles_dir, except
    the one with profile_id == exclude_id (if provided).

    Called before marking a new profile as default.
    """
    if not profiles_dir.exists():
        return

    for path in profiles_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            pid  = data.get("profile_id", path.stem)
            if exclude_id is not None and pid == exclude_id:
                continue
            if data.get("is_default"):
                data["is_default"] = False
                _atomic_write(path, data)
        except Exception:
            continue
