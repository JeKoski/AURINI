#!/usr/bin/env python3
"""
run_aurini.py — AURINI end-to-end test script (install mode)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Wires Runner + Instance + Profile + the real llama-cpp plugin together
and drives the full check → remedy → summary → execute flow interactively.

Run from the project root:

    python3 run_aurini.py

Options (edit the CONFIG section below before running):
    INSTALL_PATH  — where llama.cpp will be cloned/built
    DISPLAY_NAME  — name for this instance shown in logs
    FP16          — FP16 compute (strongly recommended for Arc A750)
    BUILD_CORES   — parallel cmake jobs (None = auto-detect)
    DRY_RUN       — True = run checks and show summary but do NOT execute build

What this script does:
    1. Creates (or loads) an AURINI instance record for llama-cpp
    2. Loads the llama-cpp plugin (auto-detects Arc on Linux → sycl_linux backend)
    3. Sets the install path on the plugin before checks run
    4. Runs all pre-flight checks and prints results
    5. For failing checks:
         - Low-risk  → offers to auto-fix (asks confirmation)
         - High-risk → explains risk, asks confirmation before applying
         - Manual    → prints instructions, asks if you want to continue anyway
    6. Prints the full summary
    7. If all checks pass and DRY_RUN is False → asks for final confirmation then builds
    8. On completion, creates a default launch profile

This is the first real end-to-end test of the full stack. Run it, see what
breaks, note it in CLAUDE.md, fix it next session.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

# ── Make project root importable ──────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

# ── CONFIG — edit before running ──────────────────────────────────────────────

INSTALL_PATH  = Path("~/llama.cpp_test").expanduser()   # Where llama.cpp will be cloned
DISPLAY_NAME  = "llama.cpp — Arc"                  # Instance display name
FP16          = True                                # FP16 compute (recommended for 8GB cards)
BUILD_CORES   = None                                # None = auto (half of cpu_count)
DRY_RUN       = False                               # True = checks only, no build

# ── Imports ────────────────────────────────────────────────────────────────────

from aurini.core.instance import Instance, PathMode
from aurini.core.profile  import Profile
from aurini.core.runner   import Runner, RunnerPhase
from aurini.core.log      import JobAction
from aurini.core.paths    import aurini_instances_dir, aurini_logs_dir


# ── Helpers ────────────────────────────────────────────────────────────────────

WIDTH = 72

def hr(char="─"):
    print(char * WIDTH)

def section(title: str):
    hr()
    print(f"  {title}")
    hr()

def ask(prompt: str, default: str = "y") -> bool:
    hint = "[Y/n]" if default == "y" else "[y/N]"
    try:
        answer = input(f"  {prompt} {hint} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if not answer:
        return default == "y"
    return answer in ("y", "yes")

def result_icon(passed: bool) -> str:
    return "✓" if passed else "✗"

def risk_label(risk: str | None) -> str:
    if risk == "low":    return "[auto-fix]"
    if risk == "high":   return "[requires confirmation]"
    if risk == "manual": return "[manual — instructions only]"
    return ""


# ── Plugin loader (importlib — hyphenated folder name) ─────────────────────────

def load_plugin(install_path: Path):
    """
    Load the llama-cpp plugin via importlib.
    The folder is named 'llama-cpp' which Python can't import with a normal
    import statement — importlib is the correct and documented pattern here.
    """
    plugin_path = PROJECT_ROOT / "plugins" / "llama-cpp" / "plugin.py"
    if not plugin_path.exists():
        print(f"\n  ERROR: Plugin not found at {plugin_path}")
        print("  Make sure you're running this from the project root.")
        sys.exit(1)

    spec   = importlib.util.spec_from_file_location("llama_cpp_plugin", plugin_path)
    module = importlib.util.module_from_spec(spec)

    # Register the backends sub-package so relative imports inside plugin.py work
    backends_init = PROJECT_ROOT / "plugins" / "llama-cpp" / "backends" / "__init__.py"
    backends_spec = importlib.util.spec_from_file_location(
        "plugins.llama_cpp.backends", backends_init
    )
    backends_mod = importlib.util.module_from_spec(backends_spec)
    sys.modules["plugins.llama_cpp.backends"] = backends_mod
    backends_spec.loader.exec_module(backends_mod)

    # Also register the parent package stubs so plugin.py's imports resolve
    for mod_name, init_path in [
        ("plugins",           PROJECT_ROOT / "plugins" / "__init__.py"),
        ("plugins.llama_cpp", PROJECT_ROOT / "plugins" / "llama-cpp" / "__init__.py"),
    ]:
        if mod_name not in sys.modules:
            s = importlib.util.spec_from_file_location(mod_name, init_path)
            m = importlib.util.module_from_spec(s)
            sys.modules[mod_name] = m
            s.loader.exec_module(m)

    sys.modules["llama_cpp_plugin"] = module
    spec.loader.exec_module(module)

    plugin = module.load(install_path=str(install_path))
    return plugin


# ── Instance setup ─────────────────────────────────────────────────────────────

def get_or_create_instance() -> Instance:
    """
    Load the existing llama-cpp-arc instance if it exists, or create a new one.
    On a fresh machine this always creates. On re-runs it loads the existing record.
    """
    instances_dir = aurini_instances_dir()
    existing = Instance.list_all(plugin_id="llama-cpp", instances_dir=instances_dir)

    # Look for one that matches our install path on the current OS
    for inst in existing:
        ip = inst.resolve_install_path()
        if ip and ip == INSTALL_PATH.resolve():
            print(f"  Loaded existing instance: {inst.instance_id}")
            return inst

    # None matched — create a new one
    inst = Instance.create(
        plugin_id="llama-cpp",
        display_name=DISPLAY_NAME,
        build_settings={
            "fp16": {"enabled": True, "value": FP16},
        },
        path_mode=PathMode.CUSTOM,
        custom_paths={"linux": str(INSTALL_PATH)},
        instances_dir=instances_dir,
    )
    print(f"  Created new instance: {inst.instance_id}")
    return inst


# ── Main flow ──────────────────────────────────────────────────────────────────

def main():
    print()
    section("AURINI — llama.cpp install (Intel Arc / SYCL / Linux)")
    print()
    print(f"  Install path : {INSTALL_PATH}")
    print(f"  FP16         : {'ON' if FP16 else 'OFF'}")
    cores_display = BUILD_CORES if BUILD_CORES else "auto"
    print(f"  Build cores  : {cores_display}")
    print(f"  Dry run      : {'YES — checks only, no build' if DRY_RUN else 'NO — will build'}")
    print()

    # ── Instance ──────────────────────────────────────────────────────────────

    section("Step 1 — Instance")
    inst = get_or_create_instance()
    print()

    # ── Plugin ────────────────────────────────────────────────────────────────

    section("Step 2 — Load plugin")
    try:
        plugin = load_plugin(INSTALL_PATH)
        print(f"  Plugin loaded: {plugin.display_name}")
        print(f"  Backend      : {plugin.backend_id}")
    except RuntimeError as e:
        print(f"\n  ERROR: {e}")
        sys.exit(1)
    print()

    # Set install path on the plugin now — build_dir_writable check needs it
    plugin.set_install_path(INSTALL_PATH)

    # ── Checks ────────────────────────────────────────────────────────────────

    section("Step 3 — Pre-flight checks")
    print()

    runner = Runner(
        plugin=plugin,
        instance_id=inst.instance_id,
        logs_dir=aurini_logs_dir(),
    )

    check_results = runner.run_checks()

    passed_count = sum(1 for r in check_results if r.passed)
    failed_count = len(check_results) - passed_count

    for result in check_results:
        icon = result_icon(result.passed)
        risk = f"  {risk_label(result.risk)}" if not result.passed and result.risk else ""
        print(f"  {icon}  {result.message}{risk}")

    print()
    print(f"  {passed_count} passed, {failed_count} failed")
    print()

    # ── Remedies ──────────────────────────────────────────────────────────────

    approved_remedies: list[str] = []

    if failed_count > 0:
        section("Step 4 — Resolve failures")
        print()

        for result in check_results:
            if result.passed:
                continue

            print(f"  ✗  {result.message}")

            if result.remedy_id is None:
                print("     No automatic fix available.")
                print()
                continue

            if result.risk == "low":
                print(f"     Auto-fix available (low risk).")
                if ask("     Apply fix?"):
                    approved_remedies.append(result.remedy_id)
                else:
                    print("     Skipped.")

            elif result.risk == "high":
                print(f"     Fix available but requires elevated permissions.")
                if result.raw_output:
                    print(f"     Raw output: {result.raw_output[:200]}")
                if ask("     Apply fix? (requires sudo)"):
                    approved_remedies.append(result.remedy_id)
                else:
                    print("     Skipped.")

            elif result.risk == "manual":
                print("     Manual fix required — AURINI cannot do this automatically.")
                print()
                # Run remedy to get the instructions (it just returns them, no system change)
                remedy_result = plugin.run_remedy(result.remedy_id)
                print(f"     {remedy_result.undo_instructions}")
                print()
                print("     After following the instructions above, re-run this script.")
                print()
                if not ask("     Have you completed this step and want to continue anyway?",
                           default="n"):
                    print()
                    print("  Exiting. Re-run after completing the manual steps above.")
                    sys.exit(0)

            print()

        # Apply approved remedies
        if approved_remedies:
            print()
            section("Step 4b — Applying fixes")
            print()

            remedy_results = runner.run_remedies(approved_remedy_ids=approved_remedies)

            for rr in remedy_results:
                icon = result_icon(rr.success)
                print(f"  {icon}  {rr.message}")
                if not rr.success and rr.raw_output:
                    print(f"       Raw output: {rr.raw_output[:400]}")
            print()
        else:
            # Still need to call run_remedies() to advance runner phase
            runner.run_remedies(approved_remedy_ids=[])
    else:
        # No failures — advance phase with empty remedies list
        runner.run_remedies(approved_remedy_ids=[])

    # ── Summary ───────────────────────────────────────────────────────────────

    section("Step 5 — Summary")
    print()

    summary = runner.build_summary()

    if summary.passing:
        print(f"  Passing ({len(summary.passing)}):")
        for r in summary.passing:
            print(f"    ✓  {r.message}")

    if summary.fixed_by_remedy:
        print(f"  Fixed by remedy ({len(summary.fixed_by_remedy)}):")
        for r in summary.fixed_by_remedy:
            print(f"    ✓  {r.message}")

    if summary.still_failing:
        print(f"  Still failing ({len(summary.still_failing)}):")
        for r in summary.still_failing:
            print(f"    ✗  {r.message}")

    print()

    if not summary.ready_to_execute:
        print("  ✗  Not ready — one or more checks are still failing.")
        print("     Fix the issues above and re-run.")
        print()
        # Show any raw output from failing checks for debugging
        for r in summary.still_failing:
            if r.raw_output:
                hr("·")
                print(f"  Raw output for '{r.check_id}':")
                print()
                print(r.raw_output[:1000])
        sys.exit(1)

    print("  ✓  All checks pass — ready to build.")
    print()

    if DRY_RUN:
        print("  DRY RUN — not building. Set DRY_RUN = False to proceed.")
        print()
        sys.exit(0)

    # ── Execute ───────────────────────────────────────────────────────────────

    section("Step 6 — Build")
    print()
    print("  This will:")
    print(f"    · Clone llama.cpp into {INSTALL_PATH}")
    print(f"    · Build with SYCL (Intel Arc) — FP16={'ON' if FP16 else 'OFF'}")
    cores_str = str(BUILD_CORES) if BUILD_CORES else "auto-detected"
    print(f"    · Use {cores_str} parallel jobs")
    print()
    print("  This takes 20–30 minutes on first build.")
    print()

    if not ask("  Begin installation?", default="n"):
        print()
        print("  Cancelled.")
        sys.exit(0)

    print()
    print("  Building… (this will take a while — output is captured)")
    print("  Check the action log in ~/aurini/logs/ for live progress.")
    print()

    # Build the config dict plugin.install() expects
    config: dict = {
        "install_path": str(INSTALL_PATH),
        "fp16":         FP16,
    }
    if BUILD_CORES:
        config["cores"] = BUILD_CORES

    try:
        runner.execute(action=JobAction.INSTALL, config=config)
    except RuntimeError as e:
        print()
        print(f"  ✗  Build failed: {e}")
        print()
        if runner.job_log:
            print(f"  Job log: ~/aurini/logs/jobs/{runner.job_log.job_id}.json")
            print("  Check the log for full details and raw command output.")
        print()
        sys.exit(1)

    # ── Done ──────────────────────────────────────────────────────────────────

    section("Done!")
    print()
    print(f"  ✓  llama.cpp built successfully at {INSTALL_PATH}")
    print()

    if runner.job_log:
        print(f"  Action log: ~/aurini/logs/jobs/{runner.job_log.job_id}.json")
        print()

    # Create a default launch profile if none exists yet
    profiles = Profile.list_all(inst)
    if not profiles:
        print("  Creating default launch profile…")
        profile = Profile.create(
            instance=inst,
            display_name="Default",
            settings={
                "model_path": {"enabled": False, "value": ""},
                "ctx_size":   {"enabled": True,  "value": 4096},
                "gpu_layers": {"enabled": True,  "value": 99},
                "flash_attn": {"enabled": False, "value": False},
                "host":       {"enabled": True,  "value": "127.0.0.1"},
                "port":       {"enabled": True,  "value": 8080},
            },
            notes="Default profile — set model_path before launching.",
            make_default=True,
        )
        print(f"  Created profile: {profile.profile_id}")
        print()
        print("  Next steps:")
        print("    1. Set model_path in the Default profile to your .gguf file")
        print("    2. Run the launch script (coming soon)")
    else:
        print(f"  Existing profiles: {[p.profile_id for p in profiles]}")

    print()
    hr()
    print()


if __name__ == "__main__":
    main()
