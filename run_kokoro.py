#!/usr/bin/env python3
"""
run_kokoro.py — AURINI Kokoro TTS install script
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Runs the Kokoro plugin's check → remedy → install flow interactively.
Use this to get Kokoro installed so you can QA the SENNI TTS integration.

Run from the project root:

    python3 run_kokoro.py

Options (edit the CONFIG section below before running):
    PYTHON_PATH  — Python executable with kokoro installed, or blank for sys.executable
    VOICES_PATH  — path to your voices/ directory, or blank to auto-discover
    ESPEAK_PATH  — path to espeak-ng binary, or blank to rely on PATH
    DRY_RUN      — True = run checks only, do not install anything

What this script does:
    1. Loads the Kokoro plugin with the configured paths
    2. Runs all pre-flight checks and prints results
    3. For failing checks:
         - High-risk (pip install, apt/winget) → explains what it will do, asks confirmation
         - Manual (voices missing)             → prints download instructions, exits
    4. If all auto-fixable checks pass → runs install
    5. Prints the three config values to write into SENNI's config.json["tts"]
"""

from __future__ import annotations

import sys
from pathlib import Path

# ── Make project root importable ──────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

# ── CONFIG — edit before running ──────────────────────────────────────────────

PYTHON_PATH  = ""   # blank = use sys.executable (the Python running this script)
VOICES_PATH  = ""   # blank = auto-discover voices/ next to tts.py
ESPEAK_PATH  = ""   # blank = rely on PATH
DRY_RUN      = False

# ── Imports ────────────────────────────────────────────────────────────────────

from aurini.core.log   import JobLog, JobAction
from aurini.core.paths import aurini_logs_dir
from plugins.kokoro.plugin import load as load_kokoro_plugin


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


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print()
    section("AURINI — Kokoro TTS install")
    print()
    python_display = PYTHON_PATH if PYTHON_PATH else f"{sys.executable}  (sys.executable)"
    print(f"  Python      : {python_display}")
    print(f"  Voices path : {VOICES_PATH if VOICES_PATH else '(auto-discover)'}")
    print(f"  espeak path : {ESPEAK_PATH if ESPEAK_PATH else '(rely on PATH)'}")
    print(f"  Dry run     : {'YES — checks only, no install' if DRY_RUN else 'NO — will install'}")
    print()

    # ── Load plugin ───────────────────────────────────────────────────────────

    section("Step 1 — Load plugin")
    print()
    plugin = load_kokoro_plugin(
        python_path=PYTHON_PATH or None,
        voices_path=VOICES_PATH or None,
        espeak_path=ESPEAK_PATH or None,
    )
    ok(f"Plugin loaded: {plugin.display_name}")
    print()

    # ── Checks ────────────────────────────────────────────────────────────────

    section("Step 2 — Pre-flight checks")
    print()

    check_results = []
    for check_id in plugin.get_checks():
        result = plugin.run_check(check_id)
        check_results.append(result)
        icon = "✓" if result.passed else "✗"
        print(f"  {icon}  {result.message}")

    passed = [r for r in check_results if r.passed]
    failed = [r for r in check_results if not r.passed]
    print()
    print(f"  {len(passed)} passed, {len(failed)} failed")
    print()

    if not failed:
        ok("All checks pass — Kokoro is already installed.")
        print()
        _print_senni_config(plugin)
        return

    # ── Remedies ──────────────────────────────────────────────────────────────

    section("Step 3 — Resolve failures")
    print()

    # Separate manual failures (voices) — handle those first since they need
    # user action before anything else matters.
    manual_failures = [r for r in failed if r.risk == "manual"]
    auto_failures   = [r for r in failed if r.risk in ("low", "high")]

    for result in manual_failures:
        fail(result.message)
        print()
        print("  ┌─ Manual step required ─────────────────────────────────────")
        remedy = plugin.run_remedy(result.remedy_id)
        for line in remedy.message.splitlines():
            print(f"  │  {line}")
        print("  └────────────────────────────────────────────────────────────")
        print()
        print("  Download your voice files, then re-run this script.")
        print()
        # Only block on voices if it's the only failure — if pip installs also
        # need to run, continue and let the user sort voices out after.
        if not auto_failures:
            sys.exit(0)
        print("  (Continuing to fix the remaining issues first.)")
        print()

    approved: list[str] = []

    for result in auto_failures:
        fail(result.message)
        remedy_descriptions = {
            "remedy_pip_kokoro":    "Run: pip install kokoro",
            "remedy_pip_soundfile": "Run: pip install soundfile",
            "remedy_install_espeak": (
                "Run: sudo apt install espeak-ng"
                if sys.platform != "win32"
                else "Run: winget install espeak-ng"
            ),
        }
        description = remedy_descriptions.get(result.remedy_id, "Apply automatic fix")
        print(f"     Fix: {description}")
        if ask("     Apply?"):
            approved.append(result.remedy_id)
        else:
            print("     Skipped.")
        print()

    if not approved:
        print("  No fixes applied. Re-run after resolving the issues above.")
        print()
        sys.exit(0)

    if DRY_RUN:
        print("  DRY RUN — not applying fixes. Set DRY_RUN = False to proceed.")
        print()
        sys.exit(0)

    # ── Apply fixes ───────────────────────────────────────────────────────────

    section("Step 4 — Applying fixes")
    print()

    logs_dir = aurini_logs_dir()
    job_log  = JobLog.create(
        plugin_id="kokoro",
        instance_id="kokoro-install",
        action=JobAction.INSTALL,
        logs_dir=logs_dir,
    )
    plugin.set_job_log(job_log)

    all_succeeded = True
    for remedy_id in approved:
        print(f"  Running {remedy_id}…")
        result = plugin.run_remedy(remedy_id)
        icon = "✓" if result.success else "✗"
        print(f"  {icon}  {result.message}")
        if not result.success:
            all_succeeded = False
            if result.raw_output:
                print()
                print("  Raw output:")
                print()
                for line in result.raw_output.splitlines()[:30]:
                    print(f"    {line}")
        print()

    job_log.mark_complete() if all_succeeded else job_log.mark_failed()

    if not all_succeeded:
        fail("One or more fixes failed. Check the output above.")
        print()
        sys.exit(1)

    # ── Re-check ──────────────────────────────────────────────────────────────

    section("Step 5 — Re-checking")
    print()

    recheck_ids  = [r.check_id for r in auto_failures if r.remedy_id in approved]
    still_failed = []

    for check_id in recheck_ids:
        result = plugin.run_check(check_id)
        icon = "✓" if result.passed else "✗"
        print(f"  {icon}  {result.message}")
        if not result.passed:
            still_failed.append(result)

    print()

    if still_failed:
        fail("Some checks are still failing after applying fixes.")
        print("  Check the output above and try again.")
        print()
        sys.exit(1)

    ok("All fixed checks now pass.")
    print()

    # ── Done ──────────────────────────────────────────────────────────────────

    section("Done!")
    print()

    if manual_failures:
        print("  ⚠  Kokoro packages and espeak-ng are installed.")
        print("     Voice files still need to be downloaded manually.")
        print("     See the instructions printed above.")
        print()
    else:
        ok("Kokoro TTS is fully installed and ready.")
        print()

    _print_senni_config(plugin)


def _print_senni_config(plugin) -> None:
    """Print the three values to write into SENNI's config.json["tts"]."""
    cfg = plugin.get_senni_config()
    section("SENNI config values")
    print()
    print("  Write these into SENNI's config.json under the \"tts\" key:")
    print()
    print('  "tts": {')
    print(f'    "python_path": "{cfg["python_path"]}",')
    print(f'    "voices_path": "{cfg["voices_path"]}",')
    print(f'    "espeak_path":  "{cfg["espeak_path"]}"')
    print('  }')
    print()
    if not cfg["python_path"]:
        print("  python_path is blank — SENNI will use its own sys.executable.")
    if not cfg["voices_path"]:
        print("  voices_path is blank — SENNI will auto-discover next to tts.py.")
    if not cfg["espeak_path"]:
        print("  espeak_path is blank — SENNI will rely on PATH.")
    print()
    hr()
    print()


if __name__ == "__main__":
    main()
