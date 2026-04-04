#!/usr/bin/env python3
"""
run_launch.py — AURINI launch script
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Loads an existing llama-cpp instance and profile, then launches llama-server
with the correct environment (oneAPI on Intel, CUDA on NVIDIA, etc.).

Run from the project root:

    python3 run_launch.py

Options (edit the CONFIG section below before running):
    INSTALL_PATH  — where llama.cpp was built (must match the instance record)
    PROFILE_NAME  — profile display name to use, or None for the default profile
    MODEL_PATH    — override the model path from the profile (or None to use profile)

What this script does:
    1. Loads the existing llama-cpp instance for INSTALL_PATH
    2. Loads the requested profile (or the default/first available)
    3. Builds the launch command via plugin.build_launch_command()
    4. Builds the launch environment via plugin.build_launch_env()
    5. On Windows (SYCL): wraps the command with setvars.bat, same as SENNI
    6. On Linux  (SYCL): wraps the command with setvars.sh + exec, same as SENNI
    7. Launches llama-server and streams output live to the terminal
    8. Ctrl-C cleanly shuts down the server
"""

from __future__ import annotations

import importlib.util
import os
import signal
import subprocess
import sys
from pathlib import Path

# ── Make project root importable ──────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

# ── CONFIG — edit before running ──────────────────────────────────────────────

INSTALL_PATH  = Path("~/llama.cpp_test").expanduser()  # Must match installed instance
PROFILE_NAME  = None           # None = use default/first profile
MODEL_PATH    = Path("C:/AI/ComfyUI_Windows_portable/ComfyUI/models/text_encoders/Qwen3.5-9B-Uncensored-HauhauCS-Aggressive-Q4_K_M.gguf").expanduser()           # None = use model path from profile
                               # Example: Path("~/models/gemma-2-9b-q4.gguf").expanduser()

# ── Imports ────────────────────────────────────────────────────────────────────

from aurini.core.instance import Instance
from aurini.core.profile  import Profile
from aurini.core.paths    import aurini_instances_dir, current_os, OsKey

# ── Helpers ────────────────────────────────────────────────────────────────────

WIDTH = 72

def hr(char="─"):
    print(char * WIDTH)

def section(title: str):
    hr()
    print(f"  {title}")
    hr()

def ok(msg: str):
    print(f"  ✓  {msg}")

def fail(msg: str):
    print(f"  ✗  {msg}")


# ── Plugin loader (importlib — hyphenated folder name) ─────────────────────────

def load_plugin(install_path: Path):
    """Load the llama-cpp plugin via importlib (hyphenated folder requires this)."""
    plugin_dir  = PROJECT_ROOT / "plugins" / "llama-cpp"
    plugin_init = plugin_dir / "__init__.py"
    plugin_file = plugin_dir / "plugin.py"

    # Register the backends sub-package
    backends_init = plugin_dir / "backends" / "__init__.py"
    backends_spec = importlib.util.spec_from_file_location(
        "plugins.llama_cpp.backends", backends_init
    )
    backends_mod = importlib.util.module_from_spec(backends_spec)
    sys.modules["plugins.llama_cpp.backends"] = backends_mod
    backends_spec.loader.exec_module(backends_mod)

    # Register the plugin package
    pkg_spec = importlib.util.spec_from_file_location("plugins.llama_cpp", plugin_init)
    pkg_mod  = importlib.util.module_from_spec(pkg_spec)
    sys.modules["plugins.llama_cpp"] = pkg_mod
    pkg_spec.loader.exec_module(pkg_mod)

    # Load plugin.py
    spec   = importlib.util.spec_from_file_location("plugins.llama_cpp.plugin", plugin_file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    plugin = module.load(install_path=str(install_path))
    return plugin


# ── Instance lookup ────────────────────────────────────────────────────────────

def find_instance() -> Instance:
    """Find the existing instance for INSTALL_PATH. Exits if not found."""
    instances_dir = aurini_instances_dir()
    existing = Instance.list_all(plugin_id="llama-cpp", instances_dir=instances_dir)

    for inst in existing:
        ip = inst.resolve_install_path()
        if ip and ip == INSTALL_PATH.resolve():
            return inst

    fail(f"No llama-cpp instance found for: {INSTALL_PATH}")
    print()
    print("  Run run_aurini.py first to install llama.cpp.")
    print()
    sys.exit(1)


# ── Profile lookup ─────────────────────────────────────────────────────────────

def find_profile(inst: Instance) -> dict:
    """
    Return the profile dict to launch with.

    If PROFILE_NAME is set, find by display name. Otherwise use the default
    profile, or the first available if no default is set.
    """
    profiles = Profile.list_all(inst)

    if not profiles:
        fail("No profiles found for this instance.")
        print()
        print("  Create a profile first, or add model_path to the default profile.")
        print()
        sys.exit(1)

    if PROFILE_NAME:
        for p in profiles:
            if p.display_name == PROFILE_NAME:
                return p.to_dict()
        fail(f"Profile '{PROFILE_NAME}' not found.")
        print(f"  Available profiles: {[p.display_name for p in profiles]}")
        sys.exit(1)

    # Use default profile, or first available
    for p in profiles:
        if p.is_default:
            return p.to_dict()
    return profiles[0].to_dict()


# ── Launch ─────────────────────────────────────────────────────────────────────

def build_launch(plugin, profile: dict) -> tuple[str | list, dict]:
    """
    Build the full launch command and environment.

    Returns (command, env) ready to pass to subprocess.run/Popen.
    On Windows: command is a shell string (setvars.bat && llama-server ...)
    On Linux:   command is a shell string (. setvars.sh --force ; exec ...)
    """
    # Allow MODEL_PATH override from config
    if MODEL_PATH is not None:
        profile = dict(profile)
        settings = dict(profile.get("settings", {}))
        settings["model_path"] = {"enabled": True, "value": str(MODEL_PATH)}
        profile["settings"] = settings

    cmd_args = plugin.build_launch_command(profile)
    env      = plugin.build_launch_env(dict(os.environ))

    if current_os() == OsKey.WINDOWS:
        from plugins.llama_cpp.backends.sycl_windows import ONEAPI_SETVARS
        # Quote each argument — handles paths with spaces
        cmd_str  = " ".join(f'"{a}"' for a in cmd_args)
        full_cmd = f'"{ONEAPI_SETVARS}" intel64 --force && {cmd_str}'
        return full_cmd, env

    else:
        # Linux: source setvars.sh then exec llama-server.
        # exec replaces the shell so the PID is the actual server process
        # (important for clean shutdown via Ctrl-C).
        import shlex
        oneapi_sh = "/opt/intel/oneapi/setvars.sh"
        safe_cmd  = " ".join(shlex.quote(a) for a in cmd_args)
        full_cmd  = f". {oneapi_sh} --force ; exec {safe_cmd}"
        return full_cmd, env


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    section("AURINI — llama-server launch")
    print()
    print(f"  Install path : {INSTALL_PATH}")
    print(f"  Profile      : {PROFILE_NAME or '(default)'}")
    if MODEL_PATH:
        print(f"  Model        : {MODEL_PATH}")
    print()

    # ── Instance ──────────────────────────────────────────────────────────────

    inst = find_instance()
    ok(f"Instance: {inst.display_name} ({inst.instance_id})")

    # ── Plugin ────────────────────────────────────────────────────────────────

    plugin = load_plugin(INSTALL_PATH)
    plugin.set_install_path(INSTALL_PATH)
    ok(f"Backend:  {plugin.backend_id}")

    # ── Profile ───────────────────────────────────────────────────────────────

    profile = find_profile(inst)
    ok(f"Profile:  {profile.get('display_name', '(unnamed)')}")

    # Warn if no model path configured
    model_setting = profile.get("settings", {}).get("model_path", {})
    if not model_setting.get("enabled") or not model_setting.get("value"):
        if MODEL_PATH is None:
            print()
            fail("No model path configured in profile and MODEL_PATH is not set.")
            print()
            print("  Set MODEL_PATH in the CONFIG section of this script, or")
            print("  edit the profile to enable and set a model_path.")
            print()
            sys.exit(1)

    # ── Build command ─────────────────────────────────────────────────────────

    full_cmd, env = build_launch(plugin, profile)

    print()
    print("  Launch command:")
    if isinstance(full_cmd, str):
        # Truncate very long commands for display
        display = full_cmd if len(full_cmd) < 200 else full_cmd[:197] + "..."
        print(f"    {display}")
    else:
        for arg in full_cmd:
            print(f"    {arg}")
    print()

    # ── Launch ────────────────────────────────────────────────────────────────

    hr()
    print("  Launching llama-server — Ctrl-C to stop")
    hr()
    print()

    shell_args: dict = {"shell": True}
    if sys.platform == "win32":
        shell_args["creationflags"] = 0  # No CREATE_NO_WINDOW — we want output

    try:
        proc = subprocess.Popen(
            full_cmd,
            env=env,
            **shell_args,
        )
        proc.wait()

    except KeyboardInterrupt:
        print()
        print()
        hr()
        print("  Shutting down llama-server…")
        hr()
        try:
            if sys.platform == "win32":
                proc.send_signal(signal.CTRL_C_EVENT)
            else:
                proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        print()
        print("  Server stopped.")
        print()

    except FileNotFoundError:
        print()
        fail(f"Binary not found: {plugin.backend.binary_path(INSTALL_PATH)}")
        print("  Has llama.cpp been built? Run run_aurini.py first.")
        print()
        sys.exit(1)


if __name__ == "__main__":
    main()
