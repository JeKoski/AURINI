"""
plugins/kokoro/plugin.py
~~~~~~~~~~~~~~~~~~~~~~~~
AURINI plugin for Kokoro TTS.

Kokoro is a pure-Python TTS engine. There is no build phase — install is:
  1. pip install kokoro soundfile  (into the chosen Python environment)
  2. apt install espeak-ng / winget install espeak-ng  (system package)
  3. Download .pt voice files from Hugging Face into a voices/ directory

Unlike llama-cpp there is no GPU/OS backend dispatcher — the same logic
works on all platforms. A backend layer can be introduced later if OS
differences grow, but for now a single plugin.py is appropriate.

The three config values SENNI needs are returned by get_senni_config():
    python_path  — path to Python with kokoro installed  (empty = sys.executable)
    voices_path  — path to voices/ dir with .pt files    (empty = auto-discover)
    espeak_path  — path to espeak-ng binary               (empty = rely on PATH)

Usage (by the core or a run script):

    from plugins.kokoro.plugin import load
    p = load(python_path=None, voices_path=None, espeak_path=None)
    p.set_job_log(job_log)

    for check_id in p.get_checks():
        result = p.run_check(check_id)
        ...

    p.install(config)
    cfg = p.get_senni_config()
    # → {"python_path": "...", "voices_path": "...", "espeak_path": "..."}
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any

from aurini.core.base import AuriniPlugin, CheckResult, RemedyResult
from aurini.core.log import JobLog, RevertType


# ── Helpers ────────────────────────────────────────────────────────────────────

def _run(command: list[str], timeout: int = 60) -> tuple[int, str]:
    """Run a command and return (returncode, combined stdout+stderr). Never raises."""
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        combined = (result.stdout or "") + (result.stderr or "")
        return result.returncode, combined.strip()
    except Exception:
        return -1, traceback.format_exc()


def _ok(check_id: str, message: str, raw: str = "", meta: dict | None = None) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        passed=True,
        message=message,
        raw_output=raw,
        metadata=meta or {},
    )


def _fail(
    check_id: str,
    message: str,
    remedy_id: str | None = None,
    risk: str | None = None,
    raw: str = "",
    meta: dict | None = None,
) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        passed=False,
        message=message,
        remedy_id=remedy_id,
        risk=risk,
        raw_output=raw,
        metadata=meta or {},
    )


# ── Plugin ─────────────────────────────────────────────────────────────────────

class KokoroPlugin(AuriniPlugin):
    """
    AURINI plugin for Kokoro TTS.

    python_path, voices_path, and espeak_path may all be None/empty at
    construction time — they come from SENNI's config UI. AURINI reads them
    before running checks and prompts the user to set them if not present.
    """

    def __init__(
        self,
        python_path: str | Path | None = None,
        voices_path: str | Path | None = None,
        espeak_path: str | Path | None = None,
    ) -> None:
        self._python_path: str = str(python_path) if python_path else ""
        self._voices_path: str = str(voices_path) if voices_path else ""
        self._espeak_path: str = str(espeak_path) if espeak_path else ""
        self._job_log: JobLog | None = None

    # ── Factory ────────────────────────────────────────────────────────────────

    @classmethod
    def load(
        cls,
        python_path: str | Path | None = None,
        voices_path: str | Path | None = None,
        espeak_path: str | Path | None = None,
    ) -> "KokoroPlugin":
        """Return a configured KokoroPlugin ready for use."""
        return cls(
            python_path=python_path,
            voices_path=voices_path,
            espeak_path=espeak_path,
        )

    # ── Identity ───────────────────────────────────────────────────────────────

    @property
    def plugin_id(self) -> str:
        return "kokoro"

    @property
    def display_name(self) -> str:
        return "Kokoro TTS"

    # ── Configuration ──────────────────────────────────────────────────────────

    def set_job_log(self, job_log: JobLog) -> None:
        """Inject the active JobLog. Call before any system-modifying method."""
        self._job_log = job_log

    def set_python_path(self, path: str | Path) -> None:
        self._python_path = str(path)

    def set_voices_path(self, path: str | Path) -> None:
        self._voices_path = str(path)

    def set_espeak_path(self, path: str | Path) -> None:
        self._espeak_path = str(path)

    # ── Resolved paths ─────────────────────────────────────────────────────────

    def _resolved_python(self) -> str:
        """
        Return the Python executable to use.
        Falls back to sys.executable if python_path is not set.
        """
        return self._python_path if self._python_path else sys.executable

    def _resolved_espeak(self) -> str | None:
        """
        Return the espeak-ng binary path, or None if not configured
        (caller should rely on PATH in that case).
        """
        return self._espeak_path if self._espeak_path else None

    def _resolved_voices(self) -> Path | None:
        """
        Return the voices directory path.
        If not configured, attempts auto-discovery next to this file.
        Returns None if nothing is configured or discoverable.
        """
        if self._voices_path:
            return Path(self._voices_path).expanduser().resolve()

        # Auto-discover: look for a 'voices/' directory next to plugin.py
        here = Path(__file__).resolve().parent
        candidate = here / "voices"
        if candidate.is_dir():
            return candidate

        return None

    # ── Pre-flight checks ──────────────────────────────────────────────────────

    def get_checks(self) -> list[str]:
        return [
            "python_usable",
            "kokoro_importable",
            "soundfile_importable",
            "espeak_present",
            "voices_dir_exists",
        ]

    def run_check(self, check_id: str) -> CheckResult:
        try:
            if check_id == "python_usable":
                return self._check_python_usable()
            if check_id == "kokoro_importable":
                return self._check_module_importable(
                    check_id="kokoro_importable",
                    module="kokoro",
                    remedy_id="remedy_pip_kokoro",
                )
            if check_id == "soundfile_importable":
                return self._check_module_importable(
                    check_id="soundfile_importable",
                    module="soundfile",
                    remedy_id="remedy_pip_soundfile",
                )
            if check_id == "espeak_present":
                return self._check_espeak_present()
            if check_id == "voices_dir_exists":
                return self._check_voices_dir()

            return _fail(
                check_id=check_id,
                message=f"Unknown check ID: {check_id!r}",
                raw="No check implemented for this ID.",
            )
        except Exception:
            return _fail(
                check_id=check_id,
                message=f"Check {check_id!r} raised an unexpected error.",
                raw=traceback.format_exc(),
            )

    def _check_python_usable(self) -> CheckResult:
        python = self._resolved_python()
        rc, raw = _run([python, "--version"])
        if rc == 0:
            version = raw.strip()
            return _ok(
                check_id="python_usable",
                message=f"Python is usable: {version} ({python})",
                raw=raw,
                meta={"python_path": python, "version_string": version},
            )
        return _fail(
            check_id="python_usable",
            message=(
                f"Python not usable at: {python}\n"
                "If python_path is blank, sys.executable was used. "
                "This is unexpected — check your Python installation."
            ),
            raw=raw,
        )

    def _check_module_importable(
        self,
        check_id: str,
        module: str,
        remedy_id: str,
    ) -> CheckResult:
        """Check that a Python module can be imported via the configured Python."""
        python = self._resolved_python()
        rc, raw = _run([python, "-c", f"import {module}; print('ok')"])
        if rc == 0 and "ok" in raw:
            return _ok(
                check_id=check_id,
                message=f"Python package '{module}' is installed and importable.",
                raw=raw,
            )
        return _fail(
            check_id=check_id,
            message=f"Python package '{module}' is not installed or not importable.",
            remedy_id=remedy_id,
            risk="high",
            raw=raw,
        )

    def _check_espeak_present(self) -> CheckResult:
        """Check that espeak-ng is available, either at espeak_path or on PATH."""
        espeak = self._resolved_espeak()

        if espeak:
            # User has configured an explicit path — verify it directly
            candidate = Path(espeak)
            if candidate.is_file() and os.access(candidate, os.X_OK):
                rc, raw = _run([espeak, "--version"])
                if rc == 0:
                    return _ok(
                        check_id="espeak_present",
                        message=f"espeak-ng found at configured path: {espeak}",
                        raw=raw,
                        meta={"espeak_path": espeak},
                    )
                return _fail(
                    check_id="espeak_present",
                    message=f"espeak-ng binary at {espeak} exists but does not run correctly.",
                    remedy_id="remedy_install_espeak",
                    risk="high",
                    raw=raw,
                )
            return _fail(
                check_id="espeak_present",
                message=f"espeak-ng not found at configured path: {espeak}",
                remedy_id="remedy_install_espeak",
                risk="high",
            )

        # No explicit path — check PATH
        found = shutil.which("espeak-ng")
        if found:
            rc, raw = _run(["espeak-ng", "--version"])
            if rc == 0:
                return _ok(
                    check_id="espeak_present",
                    message=f"espeak-ng found on PATH: {found}",
                    raw=raw,
                    meta={"espeak_path": found},
                )

        return _fail(
            check_id="espeak_present",
            message=(
                "espeak-ng is not installed or not on PATH. "
                "It is required by Kokoro for phoneme generation."
            ),
            remedy_id="remedy_install_espeak",
            risk="high",
        )

    def _check_voices_dir(self) -> CheckResult:
        voices = self._resolved_voices()
        if voices is None:
            return _fail(
                check_id="voices_dir_exists",
                message=(
                    "No voices directory is configured and auto-discovery found nothing. "
                    "Set voices_path in SENNI's Settings → Server → Voice section, "
                    "or download voice files next to SENNI's scripts/tts.py."
                ),
                remedy_id="remedy_voices_dir_missing",
                risk="manual",
            )

        if not voices.is_dir():
            return _fail(
                check_id="voices_dir_exists",
                message=f"Voices directory does not exist: {voices}",
                remedy_id="remedy_voices_dir_missing",
                risk="manual",
            )

        pt_files = list(voices.glob("*.pt"))
        if not pt_files:
            return _fail(
                check_id="voices_dir_exists",
                message=(
                    f"Voices directory exists but contains no .pt files: {voices}\n"
                    "Download voice files from "
                    "https://huggingface.co/hexgrad/Kokoro-82M/tree/main/voices"
                ),
                remedy_id="remedy_voices_dir_missing",
                risk="manual",
            )

        names = sorted(p.stem for p in pt_files)
        return _ok(
            check_id="voices_dir_exists",
            message=f"Found {len(pt_files)} voice file(s) in {voices}: {', '.join(names[:5])}{'…' if len(names) > 5 else ''}",
            meta={"voices_path": str(voices), "voice_count": len(pt_files), "voices": names},
        )

    # ── Remedies ───────────────────────────────────────────────────────────────

    def run_remedy(self, remedy_id: str) -> RemedyResult:
        try:
            if remedy_id == "remedy_pip_kokoro":
                return self._remedy_pip_install("kokoro", remedy_id)
            if remedy_id == "remedy_pip_soundfile":
                return self._remedy_pip_install("soundfile", remedy_id)
            if remedy_id == "remedy_install_espeak":
                return self._remedy_install_espeak()
            if remedy_id == "remedy_voices_dir_missing":
                # Manual — no auto-fix. Return a result pointing the user at instructions.
                return RemedyResult(
                    remedy_id=remedy_id,
                    success=False,
                    message=(
                        "Voice files cannot be downloaded automatically — "
                        "they must be downloaded manually from Hugging Face."
                    ),
                    undo_instructions="No system changes were made.",
                    raw_output="",
                )
            return RemedyResult(
                remedy_id=remedy_id,
                success=False,
                message=f"Unknown remedy ID: {remedy_id!r}",
                undo_instructions="No system changes were made.",
                raw_output="",
            )
        except Exception:
            return RemedyResult(
                remedy_id=remedy_id,
                success=False,
                message=f"Remedy {remedy_id!r} raised an unexpected error.",
                undo_instructions="Check the raw output for details. No changes may have been made.",
                raw_output=traceback.format_exc(),
            )

    def _remedy_pip_install(self, package: str, remedy_id: str) -> RemedyResult:
        python = self._resolved_python()
        command = [python, "-m", "pip", "install", package]
        rc, raw = _run(command, timeout=300)

        if rc == 0:
            self._log_action(
                description=f"Installed Python package '{package}' via pip.",
                revert_type=RevertType.AUTO,
                revert_command=[python, "-m", "pip", "uninstall", package, "-y"],
                revert_note=f"Run: {python} -m pip uninstall {package} -y",
                raw_output=raw,
            )
            return RemedyResult(
                remedy_id=remedy_id,
                success=True,
                message=f"Installed '{package}' via pip into {python}.",
                undo_instructions=f"Run: {python} -m pip uninstall {package} -y",
                raw_output=raw,
            )

        return RemedyResult(
            remedy_id=remedy_id,
            success=False,
            message=f"pip install {package} failed. See raw output for details.",
            undo_instructions="pip install did not succeed — no changes to undo.",
            raw_output=raw,
        )

    def _remedy_install_espeak(self) -> RemedyResult:
        system = platform.system()

        if system == "Linux":
            command = ["sudo", "apt", "install", "-y", "espeak-ng"]
            undo = "sudo apt remove espeak-ng"
        elif system == "Windows":
            command = ["winget", "install", "--id", "eSpeak.espeak-ng", "--silent"]
            undo = "winget uninstall --id eSpeak.espeak-ng"
        else:
            return RemedyResult(
                remedy_id="remedy_install_espeak",
                success=False,
                message=(
                    f"Automatic espeak-ng install is not supported on {system}. "
                    "Install it manually and try again."
                ),
                undo_instructions="No changes were made.",
                raw_output="",
            )

        rc, raw = _run(command, timeout=120)

        if rc == 0:
            self._log_action(
                description=f"Installed espeak-ng via {'apt' if system == 'Linux' else 'winget'}.",
                revert_type=RevertType.AUTO,
                revert_command=undo.split(),
                revert_note=undo,
                raw_output=raw,
            )
            return RemedyResult(
                remedy_id="remedy_install_espeak",
                success=True,
                message="espeak-ng installed successfully.",
                undo_instructions=undo,
                raw_output=raw,
            )

        return RemedyResult(
            remedy_id="remedy_install_espeak",
            success=False,
            message=(
                f"espeak-ng install failed (exit code {rc}). "
                "See raw output for details."
            ),
            undo_instructions="Install did not complete — check raw output.",
            raw_output=raw,
        )

    # ── Install ────────────────────────────────────────────────────────────────

    def install(self, config: dict[str, Any]) -> None:
        """
        Install Kokoro TTS dependencies.

        This runs the same steps as the remedies but in a single confirmed flow.
        config keys (all optional):
            python_path (str)  — override python executable
            voices_path (str)  — where to expect/find voice files
            espeak_path (str)  — override espeak-ng path

        All three paths are written into SENNI's config via get_senni_config()
        after install completes.

        Note: voice files are not downloaded here — they must be placed manually.
        install() will succeed even if no voice files are present; the voices check
        will surface that separately.
        """
        self._require_job_log("install")

        if config.get("python_path"):
            self.set_python_path(config["python_path"])
        if config.get("voices_path"):
            self.set_voices_path(config["voices_path"])
        if config.get("espeak_path"):
            self.set_espeak_path(config["espeak_path"])

        python = self._resolved_python()

        # Install kokoro
        rc, raw = _run([python, "-m", "pip", "install", "kokoro"], timeout=300)
        if rc != 0:
            raise RuntimeError(
                f"pip install kokoro failed.\n\n{raw}"
            )
        self._log_action(
            description="Installed kokoro Python package via pip.",
            revert_type=RevertType.AUTO,
            revert_command=[python, "-m", "pip", "uninstall", "kokoro", "-y"],
            revert_note=f"Run: {python} -m pip uninstall kokoro -y",
            raw_output=raw,
        )

        # Install soundfile
        rc, raw = _run([python, "-m", "pip", "install", "soundfile"], timeout=120)
        if rc != 0:
            raise RuntimeError(
                f"pip install soundfile failed.\n\n{raw}"
            )
        self._log_action(
            description="Installed soundfile Python package via pip.",
            revert_type=RevertType.AUTO,
            revert_command=[python, "-m", "pip", "uninstall", "soundfile", "-y"],
            revert_note=f"Run: {python} -m pip uninstall soundfile -y",
            raw_output=raw,
        )

        # Install espeak-ng
        espeak_result = self._remedy_install_espeak()
        if not espeak_result.success:
            raise RuntimeError(
                f"espeak-ng install failed.\n\n{espeak_result.raw_output}"
            )

    # ── Update ─────────────────────────────────────────────────────────────────

    def update(self, config: dict[str, Any]) -> None:
        """
        Update Kokoro by running pip install --upgrade kokoro soundfile.
        espeak-ng is a system package and is not upgraded here — system updates
        handle it.
        """
        self._require_job_log("update")

        if config.get("python_path"):
            self.set_python_path(config["python_path"])

        python = self._resolved_python()

        rc, raw = _run(
            [python, "-m", "pip", "install", "--upgrade", "kokoro", "soundfile"],
            timeout=300,
        )
        if rc != 0:
            raise RuntimeError(
                f"pip upgrade kokoro soundfile failed.\n\n{raw}"
            )
        self._log_action(
            description="Upgraded kokoro and soundfile Python packages via pip.",
            revert_type=RevertType.MANUAL,
            revert_note=(
                "To revert to a specific version:\n"
                f"  {python} -m pip install kokoro==<version> soundfile==<version>\n"
                "Check pip show kokoro for the version that was previously installed."
            ),
            raw_output=raw,
        )

    # ── Uninstall ──────────────────────────────────────────────────────────────

    def uninstall(self) -> None:
        """
        Uninstall kokoro and soundfile via pip.
        espeak-ng is a system package and is not removed — it may be used
        by other software and removing it without asking is too risky.
        """
        self._require_job_log("uninstall")

        python = self._resolved_python()

        rc, raw = _run(
            [python, "-m", "pip", "uninstall", "kokoro", "soundfile", "-y"],
            timeout=120,
        )
        self._log_action(
            description="Uninstalled kokoro and soundfile Python packages via pip.",
            revert_type=RevertType.MANUAL,
            revert_note=(
                f"To reinstall: {python} -m pip install kokoro soundfile\n"
                "Voice files in the voices/ directory are not affected."
            ),
            raw_output=raw,
        )
        if rc != 0:
            raise RuntimeError(
                f"pip uninstall kokoro soundfile failed.\n\n{raw}\n"
                "You may need to uninstall manually."
            )

    # ── Launch ─────────────────────────────────────────────────────────────────

    def build_launch_command(self, profile: dict[str, Any]) -> list[str]:
        """
        Kokoro has no standalone server process — it runs as a subprocess
        owned by SENNI's tts_server.py. The launch command is constructed
        by SENNI from tts.py directly.

        This method satisfies the AuriniPlugin ABC but is not used by SENNI.
        Raises NotImplementedError so any accidental caller gets a clear message.
        """
        raise NotImplementedError(
            "KokoroPlugin does not expose a launch command. "
            "Kokoro is managed as a subprocess by SENNI's tts_server.py. "
            "Use get_senni_config() to retrieve the path values SENNI needs."
        )

    # ── SENNI integration ──────────────────────────────────────────────────────

    def get_senni_config(self) -> dict[str, str]:
        """
        Return the three path values SENNI needs in its config.json["tts"].

        All values are strings. Empty string means "use default / rely on PATH".
        The caller (or SENNI's settings UI) writes these into config.json.

        Returned dict:
            python_path  — path to Python with kokoro installed  (empty = sys.executable)
            voices_path  — path to voices/ dir                   (empty = auto-discover)
            espeak_path  — path to espeak-ng binary               (empty = use PATH)
        """
        voices = self._resolved_voices()
        return {
            "python_path": self._python_path,
            "voices_path": str(voices) if voices else self._voices_path,
            "espeak_path": self._espeak_path,
        }

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
                f"KokoroPlugin.{method}() called without a JobLog. "
                "Call set_job_log(job_log) before any system-modifying method."
            )


# ── Convenience loader ─────────────────────────────────────────────────────────

def load(
    python_path: str | Path | None = None,
    voices_path: str | Path | None = None,
    espeak_path: str | Path | None = None,
) -> KokoroPlugin:
    """
    Module-level convenience function. Equivalent to KokoroPlugin.load().

        from plugins.kokoro import plugin as kokoro_plugin
        p = kokoro_plugin.load(
            python_path="/home/alice/.venv/bin/python",
            voices_path="/home/alice/kokoro/voices",
        )
    """
    return KokoroPlugin.load(
        python_path=python_path,
        voices_path=voices_path,
        espeak_path=espeak_path,
    )
