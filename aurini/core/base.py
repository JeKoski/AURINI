"""
aurini.core.base
~~~~~~~~~~~~~~~~
The plugin contract for AURINI.

This file is the blast radius boundary — any change here potentially touches
every plugin. Changes to these interfaces must be flagged explicitly, made
deliberately, and documented with reasoning in CLAUDE.md.

Plugins import from here:
    from aurini.core.base import AuriniPlugin, CheckResult, RemedyResult
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any


# ── Result types ───────────────────────────────────────────────────────────────

@dataclass
class CheckResult:
    """
    The outcome of a single pre-flight check.

    Every check — whether using a core library type or a custom plugin callable
    — must return one of these. The GUI uses this to build the summary screen
    and decide what remedies to offer.

    Raw output is non-negotiable. It is always captured, never discarded, and
    always available to the user via the GUI. This is what makes debugging
    possible when something breaks unexpectedly, including cases where tool
    output format changes (e.g. if Intel changes 'level_zero:gpu' to something
    else in a future update).
    """

    # The check ID as declared in plugin.json (or the plugin's get_checks list).
    check_id: str

    # Whether the check passed.
    passed: bool

    # Human-readable interpreted result — shown to the user in the summary screen.
    # Write this as if explaining to a non-technical user what was found.
    # Good:    "Intel oneAPI found at /opt/intel/oneapi/setvars.sh"
    # Good:    "Git is not installed — it is needed to download llama.cpp"
    # Avoid:   "FileNotFoundError: /opt/intel/oneapi/setvars.sh"
    message: str

    # Full raw stdout + stderr of any command(s) run during this check.
    # If no command was run (e.g. a pure Python check), set to "".
    # The GUI must always offer a "Show raw output" button — never hide this.
    raw_output: str

    # Which remedy ID to offer the user if this check failed.
    # None means AURINI cannot fix this automatically — instructions only.
    remedy_id: str | None = None

    # Risk level of the associated remedy, if one exists.
    # "low"    — AURINI can auto-fix (e.g. creating a directory). Reports what it did.
    # "high"   — AURINI asks permission first, explains risk and how to revert.
    # "manual" — AURINI cannot fix it. Shows instructions, lets user proceed at own risk.
    # None     — no remedy available.
    risk: str | None = None

    # Optional structured metadata a plugin wants to pass forward.
    # The core and GUI will not interpret this — it is for plugin-internal use only
    # (e.g. a detected version string a later check or the install step can read).
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        valid_risks = {"low", "high", "manual", None}
        if self.risk not in valid_risks:
            raise ValueError(
                f"CheckResult.risk must be one of {valid_risks}, got {self.risk!r}"
            )
        if self.remedy_id is None and self.risk is not None:
            raise ValueError(
                "CheckResult.risk is set but remedy_id is None. "
                "A risk level only makes sense when a remedy exists."
            )


@dataclass
class RemedyResult:
    """
    The outcome of an attempted remedy.

    Every remedy — whether auto-applied or user-initiated — must return one of
    these. The action log records every RemedyResult so the user can always
    see what AURINI did and how to undo it.

    undo_instructions is non-negotiable. Even for low-risk auto-fixes, the user
    must always be able to reverse what AURINI did. Write undo_instructions as
    a concrete shell command or step-by-step instruction, not a vague description.
    """

    # The remedy ID as declared in plugin.json or the plugin's run_remedy method.
    remedy_id: str

    # Whether the remedy succeeded.
    success: bool

    # What was done, in plain English. Shown to the user in the action log and
    # the summary screen.
    # Good:    "Added user 'alice' to groups: render, video"
    # Good:    "Created directory /home/alice/llama.cpp"
    # Avoid:   "subprocess.run(['usermod', ...]) returned 0"
    message: str

    # How to reverse what this remedy did. Always required — even for low-risk
    # auto-fixes. Write as a concrete command or numbered steps, not vague prose.
    # Example: "sudo gpasswd -d $USER render && sudo gpasswd -d $USER video"
    #          "Then log out and back in for the change to take effect."
    undo_instructions: str

    # Full raw stdout + stderr of any command(s) run during this remedy.
    # If no command was run, set to "".
    raw_output: str


# ── Plugin base class ──────────────────────────────────────────────────────────

class AuriniPlugin(abc.ABC):
    """
    Abstract base class for all AURINI plugins.

    Every plugin (llama.cpp, Kokoro, Whisper, etc.) must subclass this and
    implement all abstract methods. The core calls these methods in a defined
    sequence — plugins must not assume any other order.

    Call sequence during install/update:
        1. get_checks()          — core learns what checks to run
        2. run_check(id)         — once per check ID, in order
        3. run_remedy(id)        — for any failed checks, if user approves
        4. [summary screen shown — nothing touches the system until here]
        5. install() / update()  — only after all checks pass and user confirms
        6. build_launch_command()— at launch time, per profile

    Plugins must not perform any system-modifying actions outside of
    run_remedy(), install(), update(), and uninstall(). Checks must be
    read-only — they observe, they do not change.
    """

    # ── Identity ───────────────────────────────────────────────────────────────

    @property
    @abc.abstractmethod
    def plugin_id(self) -> str:
        """
        Stable machine-readable identifier for this plugin.
        Must match the 'id' field in plugin.json.
        Example: "llama-cpp"
        """

    @property
    @abc.abstractmethod
    def display_name(self) -> str:
        """
        Human-readable name shown in the GUI.
        Example: "llama.cpp"
        """

    # ── Pre-flight checks ──────────────────────────────────────────────────────

    @abc.abstractmethod
    def get_checks(self) -> list[str]:
        """
        Return the ordered list of check IDs this plugin wants the core to run.

        The core runs checks in this order. Order matters — declare checks that
        others depend on first (e.g. check oneAPI is present before checking
        that the GPU is visible via oneAPI's sycl-ls).

        Example:
            return ["oneapi_present", "render_group", "gpu_visible", "git", "cmake"]
        """

    @abc.abstractmethod
    def run_check(self, check_id: str) -> CheckResult:
        """
        Run a single check and return the result.

        Must return a CheckResult regardless of outcome — never raise an
        unhandled exception. If the check itself errors unexpectedly, return a
        failed CheckResult with the traceback in raw_output so the user can
        see what happened.

        Checks must be read-only. Do not modify the system inside a check.
        """

    # ── Remedies ───────────────────────────────────────────────────────────────

    @abc.abstractmethod
    def run_remedy(self, remedy_id: str) -> RemedyResult:
        """
        Attempt to fix a failed check.

        Only called after the user has been shown the risk level and confirmed
        they want AURINI to proceed (for high-risk remedies) or after AURINI
        has decided to auto-fix (for low-risk remedies).

        Must return a RemedyResult regardless of outcome. Never raise an
        unhandled exception — capture failures in RemedyResult.success = False
        and RemedyResult.raw_output.

        Every RemedyResult must include undo_instructions, even on failure
        (e.g. "The remedy failed partway through — check raw output and manually
        verify the state of X before retrying.").
        """

    # ── Install / uninstall ────────────────────────────────────────────────────

    @abc.abstractmethod
    def install(self, config: dict[str, Any]) -> None:
        """
        Execute the full install flow for this plugin.

        Only called after:
        - All checks have passed (or failed checks have been remedied)
        - The summary screen has been shown
        - The user has pressed "Begin installation"

        config contains the resolved build-phase settings for this instance,
        as confirmed by the user on the summary screen.

        Raise RuntimeError (with a clear message) on unrecoverable failure.
        The core will catch this, preserve any backups, and surface the error
        to the user with the message as the primary explanation.
        """

    @abc.abstractmethod
    def update(self, config: dict[str, Any]) -> None:
        """
        Execute the full update flow for this plugin.

        Same contract as install(). Called when the user is updating an
        existing instance rather than installing fresh. The plugin is
        responsible for backing up custom/modified files before pulling,
        as the llama.cpp prototype does.
        """

    @abc.abstractmethod
    def uninstall(self) -> None:
        """
        Remove everything AURINI installed for this instance.

        Must be clean and complete — no orphaned files, no broken state.
        Must not remove anything the user placed there that AURINI did not
        create. When in doubt, warn and skip rather than delete.

        Raise RuntimeError on unrecoverable failure.
        """

    # ── Launch ─────────────────────────────────────────────────────────────────

    @abc.abstractmethod
    def build_launch_command(self, profile: dict[str, Any]) -> list[str]:
        """
        Construct the argv list to launch this plugin's process.

        profile is the active profile's settings dict (launch-phase settings
        only — build-phase settings are not passed here).

        Only enabled settings are included — the plugin must check
        setting['enabled'] before adding a flag.

        Returns a list of strings ready to pass to subprocess.run() or
        equivalent. The core handles sourcing any required environment
        (e.g. oneAPI setvars.sh) around this command — the plugin returns
        the command itself, not a shell string.

        Example return value:
            ["/home/alice/llama.cpp/build/bin/llama-server",
             "--model", "/home/alice/models/gemma-27b-q4.gguf",
             "--ctx-size", "8192",
             "--flash-attn"]
        """
