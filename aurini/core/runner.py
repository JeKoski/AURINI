"""
aurini.core.runner
~~~~~~~~~~~~~~~~~~
The core runner for AURINI.

Drives the full check → remedy → summary → execute flow for any plugin.
This is the piece that ties base.py, checks.py, log.py, and the plugin
interface together into an actual install/update/uninstall sequence.

The runner is intentionally stateless between phases. Each phase produces
a result that the caller (GUI or CLI) inspects and acts on. The runner
never makes decisions on behalf of the user — it reports what it found,
what needs attention, and what will happen. The user confirms. Then it acts.

Typical usage:

    from aurini.core.runner import Runner, RunnerPhase

    runner = Runner(plugin=my_plugin, instance_id="llama-cpp-arc")

    # Phase 1 — run all pre-flight checks
    check_results = runner.run_checks()

    # Phase 2 — apply remedies the user approved
    # (GUI inspects check_results, user confirms each remedy)
    remedy_results = runner.run_remedies(approved_remedy_ids=["remedy_install_git"])

    # Phase 3 — show summary, user presses "Begin installation"
    summary = runner.build_summary()
    # ... show summary to user, get confirmation ...

    # Phase 4 — execute
    runner.execute(action="install", config=resolved_config)

Nothing in the system is modified until execute() is called. Checks and
remedies are the only exception — remedies modify the system, but only
when the user has explicitly approved them (see run_remedies()).

Action log integration:

    runner = Runner(plugin=my_plugin, instance_id="llama-cpp-arc", logs_dir=path)

    # A JobLog is created on the first system-modifying call:
    #   run_remedies() and execute() both create/use a log.
    # The log is accessible via runner.job_log after that point.

    # To resume an existing job (e.g. picking up after a crash):
    runner = Runner.resume(job_id="...", plugin=my_plugin, logs_dir=path)
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from aurini.core.base import AuriniPlugin, CheckResult, RemedyResult
from aurini.core.log import (
    JobAction,
    JobLog,
    JobStatus,
    RevertStatus,
    RevertType,
)


# ── Phase enum ─────────────────────────────────────────────────────────────────

class RunnerPhase(str, Enum):
    """
    Tracks where the runner is in the install/update/uninstall lifecycle.

    The GUI uses this to know which screen to show and which actions are valid.
    Transitions only move forward — there is no going back within a single run.
    """
    IDLE         = "idle"          # Runner created, nothing started yet
    CHECKING     = "checking"      # Running pre-flight checks
    CHECKS_DONE  = "checks_done"   # All checks complete, awaiting remedy approvals
    REMEDYING    = "remedying"     # Applying approved remedies
    READY        = "ready"         # All checks pass, ready for summary + execute
    BLOCKED      = "blocked"       # One or more checks failed with no remedy — cannot proceed
    EXECUTING    = "executing"     # Install/update/uninstall in progress
    DONE         = "done"          # Completed successfully
    FAILED       = "failed"        # Failed during execute — see runner.error


# ── Summary dataclass ──────────────────────────────────────────────────────────

@dataclass
class RunnerSummary:
    """
    The summary the GUI shows before asking the user to confirm execution.

    Reflects the current state of checks and remedies — call build_summary()
    after all remedies have been applied to get the final version.
    """

    # All check results, in the order the plugin declared them.
    checks: list[CheckResult]

    # Remedy results for any remedies that were run.
    remedies: list[RemedyResult]

    # Checks that are still failing after all approved remedies have been applied.
    # If this list is non-empty, the runner will not allow execute().
    still_failing: list[CheckResult]

    # Checks that passed and required no remedy.
    passing: list[CheckResult]

    # Checks that passed because a remedy succeeded.
    fixed_by_remedy: list[CheckResult]

    # Checks that are failing and have no remedy (manual-only or no remedy at all).
    # The GUI shows these as "cannot be fixed automatically".
    manual_or_unfixable: list[CheckResult]

    # Whether all checks pass and execute() can proceed.
    @property
    def ready_to_execute(self) -> bool:
        return len(self.still_failing) == 0


# ── Runner ─────────────────────────────────────────────────────────────────────

class Runner:
    """
    Orchestrates the full install/update/uninstall flow for a single plugin instance.

    One Runner instance = one job. Create a new Runner for each install or update.
    """

    def __init__(
        self,
        plugin:      AuriniPlugin,
        instance_id: str,
        logs_dir:    Path | None = None,
    ) -> None:
        self.plugin      = plugin
        self.instance_id = instance_id
        self.logs_dir    = logs_dir

        self.phase: RunnerPhase = RunnerPhase.IDLE
        self.error: str | None  = None

        # Results accumulated across phases
        self._check_results:  list[CheckResult]  = []
        self._remedy_results: list[RemedyResult] = []

        # Map check_id → latest CheckResult (updated after re-checks post-remedy)
        self._check_map: dict[str, CheckResult] = {}

        # Set of check_ids that were fixed by a successful remedy
        self._fixed_by_remedy: set[str] = set()

        # The action log for this job. Created on first system-modifying call.
        self.job_log: JobLog | None = None

    # ── Factory: resume an interrupted job ────────────────────────────────────

    @classmethod
    def resume(
        cls,
        job_id:   str,
        plugin:   AuriniPlugin,
        logs_dir: Path | None = None,
    ) -> Runner:
        """
        Reconstruct a Runner for an interrupted job so reversion can continue.

        The returned runner is in FAILED phase with its job_log loaded.
        Only begin_revert() / step_revert() / finish_revert() make sense to
        call on a resumed runner.
        """
        runner = cls(plugin=plugin, instance_id="", logs_dir=logs_dir)
        runner.job_log = JobLog.load(job_id=job_id, logs_dir=logs_dir)
        runner.instance_id = runner.job_log.instance_id
        runner.phase = RunnerPhase.FAILED
        return runner

    # ── Phase 1: checks ───────────────────────────────────────────────────────

    def run_checks(self) -> list[CheckResult]:
        """
        Run all pre-flight checks declared by the plugin, in order.

        Returns the full list of CheckResult objects. The caller (GUI or CLI)
        inspects these to decide which remedies to offer the user.

        Checks are always read-only. This method never modifies the system.

        After this call, runner.phase is CHECKS_DONE.
        """
        self._assert_phase(RunnerPhase.IDLE, "run_checks")
        self.phase = RunnerPhase.CHECKING

        self._check_results = []
        self._check_map = {}

        check_ids = self.plugin.get_checks()

        for check_id in check_ids:
            try:
                result = self.plugin.run_check(check_id)
            except Exception:
                # Plugin violated the contract — run_check must not raise.
                # Capture and present as a failed check so the user sees it.
                result = CheckResult(
                    check_id=check_id,
                    passed=False,
                    message=(
                        f"AURINI internal error: plugin raised an exception during "
                        f"check '{check_id}'. This is a plugin bug. See raw output."
                    ),
                    raw_output=traceback.format_exc(),
                )
            self._check_results.append(result)
            self._check_map[check_id] = result

        self.phase = RunnerPhase.CHECKS_DONE
        return list(self._check_results)

    # ── Phase 2: remedies ─────────────────────────────────────────────────────

    def run_remedies(
        self,
        approved_remedy_ids: list[str],
    ) -> list[RemedyResult]:
        """
        Apply the remedies the user has approved.

        approved_remedy_ids is the list of remedy IDs the user has confirmed.
        The GUI is responsible for collecting this — the runner applies them in
        the order they appear in approved_remedy_ids.

        After each remedy, the corresponding check is re-run to verify the fix
        took effect. The re-check result replaces the original in _check_map.

        Low-risk remedies may be auto-approved by the caller before this call.
        High-risk remedies must have explicit user confirmation before being
        included in approved_remedy_ids. The runner does not distinguish — it
        applies whatever is in the list. The approval gate is the caller's
        responsibility.

        This method creates the JobLog if one does not already exist.

        After this call, runner.phase is READY (all checks pass) or BLOCKED
        (one or more checks still failing with no remedy available).
        """
        self._assert_phase(RunnerPhase.CHECKS_DONE, "run_remedies")
        self.phase = RunnerPhase.REMEDYING

        # Create the job log on the first system-modifying call
        if self.job_log is None:
            self.job_log = JobLog.create(
                plugin_id=self.plugin.plugin_id,
                instance_id=self.instance_id,
                action=JobAction.REMEDY,
                logs_dir=self.logs_dir,
            )
            # Inject into plugin so install/update/uninstall can log their actions
            if hasattr(self.plugin, "set_job_log"):
                self.plugin.set_job_log(self.job_log)

        # Build a map: remedy_id → list of check_ids that need it
        remedy_to_checks: dict[str, list[str]] = {}
        for result in self._check_results:
            if not result.passed and result.remedy_id in approved_remedy_ids:
                remedy_to_checks.setdefault(result.remedy_id, []).append(result.check_id)

        # Apply each approved remedy once, in the order requested
        seen_remedies: set[str] = set()
        self._remedy_results = []

        for remedy_id in approved_remedy_ids:
            if remedy_id in seen_remedies:
                continue
            seen_remedies.add(remedy_id)

            if remedy_id not in remedy_to_checks:
                # Approved remedy has no corresponding failed check — skip
                continue

            try:
                remedy_result = self.plugin.run_remedy(remedy_id)
            except Exception:
                # Plugin violated the contract — run_remedy must not raise.
                remedy_result = RemedyResult(
                    remedy_id=remedy_id,
                    success=False,
                    message=(
                        f"AURINI internal error: plugin raised an exception during "
                        f"remedy '{remedy_id}'. This is a plugin bug. See raw output."
                    ),
                    undo_instructions=(
                        "The remedy failed unexpectedly. Check raw output and verify "
                        "system state manually before retrying."
                    ),
                    raw_output=traceback.format_exc(),
                )

            self._remedy_results.append(remedy_result)

            # Log the remedy action
            self.job_log.add_entry(
                description=remedy_result.message,
                revert_type=RevertType.MANUAL,
                revert_note=remedy_result.undo_instructions,
                raw_output=remedy_result.raw_output,
            )

            # Re-run affected checks to see if the remedy worked
            if remedy_result.success:
                for check_id in remedy_to_checks.get(remedy_id, []):
                    try:
                        re_result = self.plugin.run_check(check_id)
                    except Exception:
                        re_result = CheckResult(
                            check_id=check_id,
                            passed=False,
                            message=(
                                f"AURINI internal error: plugin raised an exception during "
                                f"re-check of '{check_id}' after remedy. See raw output."
                            ),
                            raw_output=traceback.format_exc(),
                        )
                    self._check_map[check_id] = re_result
                    if re_result.passed:
                        self._fixed_by_remedy.add(check_id)

        # Determine next phase based on whether all checks now pass
        still_failing = [r for r in self._check_map.values() if not r.passed]
        self.phase = RunnerPhase.READY if not still_failing else RunnerPhase.BLOCKED

        return list(self._remedy_results)

    # ── Summary ────────────────────────────────────────────────────────────────

    def build_summary(self) -> RunnerSummary:
        """
        Build the summary that the GUI shows before asking the user to confirm.

        Can be called after run_checks() (before remedies) to show an early
        summary, or after run_remedies() for the final pre-execution summary.
        """
        if self.phase not in (
            RunnerPhase.CHECKS_DONE,
            RunnerPhase.READY,
            RunnerPhase.BLOCKED,
        ):
            raise RuntimeError(
                f"build_summary() called in phase {self.phase.value}. "
                "Call run_checks() first."
            )

        # Use the most up-to-date check results (post-remedy re-checks)
        current_results = [self._check_map.get(r.check_id, r) for r in self._check_results]

        passing        = [r for r in current_results if r.passed and r.check_id not in self._fixed_by_remedy]
        fixed          = [r for r in current_results if r.check_id in self._fixed_by_remedy]
        still_failing  = [r for r in current_results if not r.passed]
        manual_only    = [r for r in still_failing if r.remedy_id is None or r.risk == "manual"]

        return RunnerSummary(
            checks=current_results,
            remedies=list(self._remedy_results),
            still_failing=still_failing,
            passing=passing,
            fixed_by_remedy=fixed,
            manual_or_unfixable=manual_only,
        )

    # ── Phase 3: execute ───────────────────────────────────────────────────────

    def execute(
        self,
        action: JobAction | str,
        config: dict[str, Any],
    ) -> None:
        """
        Execute the install, update, or uninstall.

        Only callable when runner.phase is READY (all checks pass).
        Raises RuntimeError if called in any other phase.

        action must be "install", "update", or "uninstall".
        config is the resolved build-phase settings dict, as confirmed by the
        user on the summary screen.

        On success: runner.phase → DONE, job_log.status → COMPLETE.
        On failure: runner.phase → FAILED, runner.error set, job_log.status → FAILED.

        The JobLog is created here if it was not already created during
        run_remedies() (i.e. no remedies were needed).
        """
        if self.phase != RunnerPhase.READY:
            raise RuntimeError(
                f"execute() called in phase {self.phase.value}. "
                "All checks must pass before executing. "
                "Call run_checks(), resolve any failures, then try again."
            )

        action = JobAction(action) if isinstance(action, str) else action

        if action not in (JobAction.INSTALL, JobAction.UPDATE, JobAction.UNINSTALL):
            raise ValueError(
                f"execute() does not support action '{action.value}'. "
                "Use install, update, or uninstall."
            )

        # Create (or re-use) the job log
        if self.job_log is None or self.job_log.action == JobAction.REMEDY:
            self.job_log = JobLog.create(
                plugin_id=self.plugin.plugin_id,
                instance_id=self.instance_id,
                action=action,
                logs_dir=self.logs_dir,
            )

        if hasattr(self.plugin, "set_job_log"):
            self.plugin.set_job_log(self.job_log)

        self.phase = RunnerPhase.EXECUTING

        try:
            if action == JobAction.INSTALL:
                self.plugin.install(config)
            elif action == JobAction.UPDATE:
                self.plugin.update(config)
            elif action == JobAction.UNINSTALL:
                self.plugin.uninstall()

            self.job_log.mark_complete()
            self.phase = RunnerPhase.DONE

        except Exception as exc:
            error_msg = str(exc) if str(exc) else repr(exc)
            self.error = error_msg
            self.job_log.mark_failed(error=error_msg)
            self.phase = RunnerPhase.FAILED
            raise

    # ── Reversion ──────────────────────────────────────────────────────────────

    def begin_revert(self) -> None:
        """
        Start the reversion process for a failed or completed job.

        Marks the job log as IN_PROGRESS for reversion. The caller then calls
        next_revert_entry() / step_revert() in a loop until is_revert_done()
        returns True, then calls finish_revert().

        Can also be used to undo a successfully completed job (the user changed
        their mind). In that case, pass force=True.
        """
        if self.job_log is None:
            raise RuntimeError("No job log — nothing to revert.")
        self.job_log.begin_revert()

    def next_revert_entry(self):
        """
        Return the next LogEntry to revert, or None if reversion is complete.

        The caller uses this to drive the reversion UI: show the user what will
        be done, collect confirmation, then call step_revert().
        """
        if self.job_log is None:
            return None
        return self.job_log.next_revert_entry()

    def step_revert(
        self,
        entry_id:     int,
        confirmed:    bool,
        raw_output:   str = "",
        outcome_note: str = "",
    ) -> None:
        """
        Mark one log entry as reverted (or skipped/failed).

        confirmed=True  → mark as COMPLETE (revert was performed and confirmed)
        confirmed=False → mark as SKIPPED (user chose to skip this entry)

        For AUTO entries, the caller is responsible for actually running the
        revert_command before calling this. The runner records the outcome —
        it does not execute commands itself during reversion (the GUI drives that).

        For MANUAL entries, confirmed means the user has confirmed they performed
        the manual step.

        outcome_note is recorded for failures or skips so the user can see why.
        """
        if self.job_log is None:
            raise RuntimeError("No job log — nothing to revert.")

        status = RevertStatus.COMPLETE if confirmed else RevertStatus.SKIPPED
        self.job_log.mark_entry_revert_status(
            entry_id=entry_id,
            status=status,
            raw_output=raw_output,
            outcome_note=outcome_note,
        )

    def pause_revert_awaiting_user(self) -> None:
        """
        Pause reversion because a manual step requires the user to act
        (e.g. log out and back in) before reversion can continue.

        AURINI will surface this job on next startup so reversion can resume.
        Call resume_revert() when the user confirms the manual step is done.
        """
        if self.job_log is None:
            raise RuntimeError("No job log.")
        self.job_log.pause_revert_awaiting_user()

    def resume_revert(self) -> None:
        """Resume reversion after a paused manual step has been completed."""
        if self.job_log is None:
            raise RuntimeError("No job log.")
        self.job_log.resume_revert()

    def finish_revert(self) -> None:
        """
        Mark reversion as finished. Inspects entry states to determine whether
        it was fully complete or only partial. Call this after step_revert()
        returns None (no more entries to revert).
        """
        if self.job_log is None:
            raise RuntimeError("No job log.")
        self.job_log.finish_revert()

    def is_revert_done(self) -> bool:
        """True if there are no more entries left to revert."""
        if self.job_log is None:
            return True
        return not self.job_log.has_pending_revert()

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _assert_phase(self, expected: RunnerPhase, method: str) -> None:
        if self.phase != expected:
            raise RuntimeError(
                f"{method}() called in phase {self.phase.value!r}, "
                f"expected {expected.value!r}."
            )
