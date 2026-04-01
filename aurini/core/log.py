"""
aurini.core.log
~~~~~~~~~~~~~~~
Action log for AURINI.

Every action AURINI takes against the system is recorded here. The log is:
- Written to disk at every state change — survives crashes mid-install or
  mid-reversion
- The live source of truth for reversion state — not just a record of what
  happened, but where the reversion process is right now
- Structured so the GUI can display job history and drive the undo flow
- Indexed globally so AURINI can detect incomplete reversions on startup
  and immediately surface them to the user

Directory layout:
    ~/aurini/logs/
        index.json
        jobs/
            2026-04-01_14-32-07_llama-cpp-arc_install.json
            2026-04-01_15-10-22_llama-cpp-arc_update.json

Reversion is a state machine. Each entry and each job tracks its own
reversion state independently. The runner always works backwards through
entry_id order and never removes or renumbers entries — skipped entries
stay in the list marked as skipped.

Usage:

    from aurini.core.log import JobLog, LogEntry, RevertType, RevertStatus

    # Start a new job
    log = JobLog.create(
        plugin_id="llama-cpp",
        instance_id="llama-cpp-arc",
        action="install",
    )

    # Record an action
    entry = log.add_entry(
        description="Added user 'alice' to groups: render, video",
        revert_type=RevertType.AUTO,
        revert_command=["sudo", "gpasswd", "-d", "alice", "render"],
        revert_note="Also run for the 'video' group. Log out and back in after.",
        raw_output="",
    )

    # Mark job complete
    log.mark_complete()

    # Later — begin reversion
    log.begin_revert()
    for entry in log.revert_order():
        # ... execute revert ...
        log.mark_entry_revert_status(entry.entry_id, RevertStatus.COMPLETE)
    log.finish_revert()
"""

from __future__ import annotations

import json
import os
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Iterator


# ── Enums ──────────────────────────────────────────────────────────────────────

class JobAction(str, Enum):
    INSTALL   = "install"
    UPDATE    = "update"
    UNINSTALL = "uninstall"
    REMEDY    = "remedy"


class JobStatus(str, Enum):
    """Overall install/update/uninstall status of a job."""
    IN_PROGRESS = "in_progress"
    COMPLETE    = "complete"
    FAILED      = "failed"        # Failed partway through — partial state on disk


class JobRevertStatus(str, Enum):
    """Reversion progress for a job as a whole."""
    NOT_STARTED        = "not_started"
    IN_PROGRESS        = "in_progress"
    AWAITING_USER      = "awaiting_user"   # Paused — waiting for manual user action
    COMPLETE           = "complete"
    PARTIALLY_REVERTED = "partially_reverted"  # Some entries failed or were skipped


class RevertType(str, Enum):
    AUTO   = "auto"    # AURINI can run the revert command — still requires confirmation
    MANUAL = "manual"  # AURINI cannot run it — surfaces instructions, waits for user


class RevertStatus(str, Enum):
    """Reversion status of a single log entry."""
    PENDING        = "pending"
    IN_PROGRESS    = "in_progress"
    AWAITING_USER  = "awaiting_user"  # Manual step — waiting for user to confirm done
    COMPLETE       = "complete"
    FAILED         = "failed"
    SKIPPED        = "skipped"        # User chose to skip — never removed from list


# ── Log entry ──────────────────────────────────────────────────────────────────

@dataclass
class LogEntry:
    """
    A single recorded action within a job.

    entry_id is immutable once assigned — entries are never renumbered.
    The undo runner walks entries in reverse entry_id order.
    Skipped entries stay in the list with revert_status = SKIPPED.
    """

    entry_id: int

    # What was done, in plain English. Shown in the GUI and action log.
    description: str

    # Whether AURINI can execute the reversal or only surface instructions.
    revert_type: RevertType

    # For AUTO: the command to run to reverse this action.
    # The undo runner always asks for explicit user confirmation before
    # executing — auto means AURINI *can* run it, not that it does silently.
    # For MANUAL: None — instructions are in revert_note.
    revert_command: list[str] | None

    # Human-readable instructions for reverting this entry.
    # Required for MANUAL entries. Optional but recommended for AUTO entries
    # (e.g. "Log out and back in after running this command.").
    revert_note: str | None

    # Full raw stdout + stderr of any command(s) run. Never discarded.
    raw_output: str

    # Timestamp when this entry was recorded.
    timestamp: str = field(default_factory=lambda: _now())

    # Current reversion state. Persisted to disk at every transition.
    revert_status: RevertStatus = RevertStatus.PENDING

    # Raw output from the revert command, if one was run.
    revert_raw_output: str = ""

    # Free-form note recorded when reversion fails or is skipped —
    # e.g. what the user said, what the error was.
    revert_note_outcome: str = ""

    def __post_init__(self) -> None:
        # Coerce string values back to enums when deserialising from JSON
        if isinstance(self.revert_type, str):
            self.revert_type = RevertType(self.revert_type)
        if isinstance(self.revert_status, str):
            self.revert_status = RevertStatus(self.revert_status)
        if self.revert_type == RevertType.AUTO and self.revert_command is None:
            raise ValueError(
                f"LogEntry {self.entry_id}: revert_type is AUTO but revert_command is None. "
                "Provide the command, or set revert_type to MANUAL."
            )
        if self.revert_type == RevertType.MANUAL and self.revert_note is None:
            raise ValueError(
                f"LogEntry {self.entry_id}: revert_type is MANUAL but revert_note is None. "
                "Manual entries must always include revert_note so the user knows what to do."
            )

    @property
    def is_terminal(self) -> bool:
        """True if this entry's reversion is in a final state (no further transitions)."""
        return self.revert_status in (
            RevertStatus.COMPLETE,
            RevertStatus.FAILED,
            RevertStatus.SKIPPED,
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d["revert_type"]   = self.revert_type.value
        d["revert_status"] = self.revert_status.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> LogEntry:
        return cls(**d)


# ── Job log ────────────────────────────────────────────────────────────────────

@dataclass
class JobLog:
    """
    The complete log for a single install, update, uninstall, or remedy job.

    This is the live source of truth for both the job's progress and its
    reversion state. It is written to disk at every state change so it
    survives crashes mid-job or mid-reversion.

    On startup, AURINI checks the index for jobs with revert_status of
    IN_PROGRESS or AWAITING_USER and immediately surfaces them to the user
    so reversion can resume.
    """

    job_id:      str
    plugin_id:   str
    instance_id: str
    action:      JobAction
    started:     str

    status:        JobStatus       = JobStatus.IN_PROGRESS
    revert_status: JobRevertStatus = JobRevertStatus.NOT_STARTED

    completed:  str | None = None
    error:      str | None = None   # Set if status == FAILED

    entries: list[LogEntry] = field(default_factory=list)

    # Path where this job file lives — set by JobLog.create(), not stored in JSON
    _path: Path | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if isinstance(self.action, str):
            self.action = JobAction(self.action)
        if isinstance(self.status, str):
            self.status = JobStatus(self.status)
        if isinstance(self.revert_status, str):
            self.revert_status = JobRevertStatus(self.revert_status)
        if self.entries and isinstance(self.entries[0], dict):
            self.entries = [LogEntry.from_dict(e) for e in self.entries]

    # ── Factory ────────────────────────────────────────────────────────────────

    @classmethod
    def create(
        cls,
        plugin_id:   str,
        instance_id: str,
        action:      JobAction | str,
        logs_dir:    Path | None = None,
    ) -> JobLog:
        """
        Create a new job log, write it to disk, and register it in the index.

        logs_dir defaults to ~/aurini/logs/. Override for testing.
        """
        logs_dir = _resolve_logs_dir(logs_dir)
        jobs_dir = logs_dir / "jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)

        action  = JobAction(action) if isinstance(action, str) else action
        started = _now()
        job_id  = _make_job_id(started, instance_id, action)

        job = cls(
            job_id=job_id,
            plugin_id=plugin_id,
            instance_id=instance_id,
            action=action,
            started=started,
        )
        job._path = jobs_dir / f"{job_id}.json"
        job._flush()
        _index_upsert(logs_dir, job)
        return job

    @classmethod
    def load(cls, job_id: str, logs_dir: Path | None = None) -> JobLog:
        """Load an existing job log from disk by job_id."""
        logs_dir = _resolve_logs_dir(logs_dir)
        path = logs_dir / "jobs" / f"{job_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"Job log not found: {path}")
        job = cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        job._path = path
        return job

    @classmethod
    def from_dict(cls, d: dict) -> JobLog:
        entries = [LogEntry.from_dict(e) for e in d.pop("entries", [])]
        job = cls(**d, entries=entries)
        return job

    # ── Entry management ───────────────────────────────────────────────────────

    def add_entry(
        self,
        description:     str,
        revert_type:     RevertType | str,
        revert_command:  list[str] | None = None,
        revert_note:     str | None = None,
        raw_output:      str = "",
    ) -> LogEntry:
        """
        Record a new action. Assigns the next entry_id, appends to the list,
        and immediately flushes to disk.

        entry_ids are 1-based and never reused or renumbered.
        """
        revert_type = RevertType(revert_type) if isinstance(revert_type, str) else revert_type
        entry_id = (max(e.entry_id for e in self.entries) + 1) if self.entries else 1

        entry = LogEntry(
            entry_id=entry_id,
            description=description,
            revert_type=revert_type,
            revert_command=revert_command,
            revert_note=revert_note,
            raw_output=raw_output,
        )
        self.entries.append(entry)
        self._flush()
        return entry

    def mark_entry_revert_status(
        self,
        entry_id:        int,
        status:          RevertStatus | str,
        raw_output:      str = "",
        outcome_note:    str = "",
    ) -> None:
        """
        Update the reversion status of a single entry and flush to disk.

        Called by the undo runner at each step of the reversion process.
        raw_output should contain the output of the revert command if one was run.
        outcome_note is a free-form note for failures or skips.
        """
        status = RevertStatus(status) if isinstance(status, str) else status
        entry = self._get_entry(entry_id)
        entry.revert_status = status
        if raw_output:
            entry.revert_raw_output = raw_output
        if outcome_note:
            entry.revert_note_outcome = outcome_note
        self._flush()

    # ── Job-level state transitions ────────────────────────────────────────────

    def mark_complete(self) -> None:
        """Mark the job as successfully completed."""
        self.status    = JobStatus.COMPLETE
        self.completed = _now()
        self._flush()
        _index_upsert(_resolve_logs_dir(self._path.parent.parent if self._path else None), self)

    def mark_failed(self, error: str) -> None:
        """
        Mark the job as failed. error should be a clear explanation of what
        went wrong, suitable for display to the user.
        """
        self.status    = JobStatus.FAILED
        self.error     = error
        self.completed = _now()
        self._flush()
        _index_upsert(_resolve_logs_dir(self._path.parent.parent if self._path else None), self)

    def begin_revert(self) -> None:
        """Mark that reversion has started. Flushes to disk immediately."""
        self.revert_status = JobRevertStatus.IN_PROGRESS
        self._flush()
        _index_upsert(_resolve_logs_dir(self._path.parent.parent if self._path else None), self)

    def pause_revert_awaiting_user(self) -> None:
        """
        Pause reversion because a manual step requires user action.

        AURINI will surface this job on next startup so reversion can resume.
        """
        self.revert_status = JobRevertStatus.AWAITING_USER
        self._flush()
        _index_upsert(_resolve_logs_dir(self._path.parent.parent if self._path else None), self)

    def resume_revert(self) -> None:
        """Resume reversion after a manual step has been completed by the user."""
        self.revert_status = JobRevertStatus.IN_PROGRESS
        self._flush()
        _index_upsert(_resolve_logs_dir(self._path.parent.parent if self._path else None), self)

    def finish_revert(self) -> None:
        """
        Mark reversion as finished. Inspects entry states to determine whether
        reversion was fully complete or only partial.
        """
        failed_or_skipped = [
            e for e in self.entries
            if e.revert_status in (RevertStatus.FAILED, RevertStatus.SKIPPED)
        ]
        self.revert_status = (
            JobRevertStatus.PARTIALLY_REVERTED
            if failed_or_skipped
            else JobRevertStatus.COMPLETE
        )
        self._flush()
        _index_upsert(_resolve_logs_dir(self._path.parent.parent if self._path else None), self)

    # ── Reversion helpers ──────────────────────────────────────────────────────

    def revert_order(self) -> Iterator[LogEntry]:
        """
        Yield entries in reverse entry_id order, skipping already-terminal ones.

        This is the sequence the undo runner should follow. Terminal entries
        (COMPLETE, FAILED, SKIPPED) are not yielded — they are already done.
        """
        for entry in reversed(self.entries):
            if not entry.is_terminal:
                yield entry

    def has_pending_revert(self) -> bool:
        """True if any entries still need to be reverted."""
        return any(not e.is_terminal for e in self.entries)

    def next_revert_entry(self) -> LogEntry | None:
        """
        Return the next entry to revert (highest entry_id that is not terminal).
        Returns None if reversion is complete.
        """
        for entry in reversed(self.entries):
            if not entry.is_terminal:
                return entry
        return None

    # ── Serialisation ──────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "job_id":        self.job_id,
            "plugin_id":     self.plugin_id,
            "instance_id":   self.instance_id,
            "action":        self.action.value,
            "started":       self.started,
            "completed":     self.completed,
            "status":        self.status.value,
            "revert_status": self.revert_status.value,
            "error":         self.error,
            "entries":       [e.to_dict() for e in self.entries],
        }

    def _flush(self) -> None:
        """Write current state to disk atomically. Called after every mutation."""
        if self._path is None:
            return
        _atomic_write(self._path, self.to_dict())

    def _get_entry(self, entry_id: int) -> LogEntry:
        for e in self.entries:
            if e.entry_id == entry_id:
                return e
        raise KeyError(f"No entry with entry_id={entry_id} in job {self.job_id}")


# ── Index ──────────────────────────────────────────────────────────────────────

def load_index(logs_dir: Path | None = None) -> list[dict]:
    """
    Return the full job index as a list of summary dicts, newest first.

    Each dict contains: job_id, plugin_id, instance_id, action, started,
    completed, status, revert_status.
    """
    logs_dir   = _resolve_logs_dir(logs_dir)
    index_path = logs_dir / "index.json"
    if not index_path.exists():
        return []
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
        jobs = data.get("jobs", [])
        return list(reversed(jobs))  # Newest first
    except Exception:
        return []


def find_incomplete_reversions(logs_dir: Path | None = None) -> list[dict]:
    """
    Return index entries for jobs whose reversion is IN_PROGRESS or
    AWAITING_USER. These are surfaced to the user on startup so reversion
    can resume.
    """
    resumable = {JobRevertStatus.IN_PROGRESS.value, JobRevertStatus.AWAITING_USER.value}
    return [j for j in load_index(logs_dir) if j.get("revert_status") in resumable]


# ── Internal helpers ───────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_job_id(started: str, instance_id: str, action: JobAction) -> str:
    """
    Build a human-readable, filesystem-safe job ID.
    Format: 2026-04-01_14-32-07_llama-cpp-arc_install
    """
    ts = started.replace(":", "-").replace("T", "_").rstrip("Z")
    safe_instance = instance_id.replace(" ", "-")
    return f"{ts}_{safe_instance}_{action.value}"


def _resolve_logs_dir(logs_dir: Path | None) -> Path:
    if logs_dir is not None:
        return logs_dir
    return Path.home() / "aurini" / "logs"


def _atomic_write(path: Path, data: dict) -> None:
    """
    Write JSON to path atomically using a temp file + rename.

    Rename is atomic on POSIX systems — a crash mid-write leaves the previous
    file intact rather than producing a corrupt file.
    """
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        # If the write fails, leave the existing file untouched
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise


def _index_upsert(logs_dir: Path, job: JobLog) -> None:
    """
    Insert or update this job's summary entry in index.json.
    Creates the index if it doesn't exist yet.
    """
    logs_dir   = _resolve_logs_dir(logs_dir)
    index_path = logs_dir / "index.json"
    logs_dir.mkdir(parents=True, exist_ok=True)

    try:
        existing = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {}
    except Exception:
        existing = {}

    jobs: list[dict] = existing.get("jobs", [])

    summary = {
        "job_id":        job.job_id,
        "plugin_id":     job.plugin_id,
        "instance_id":   job.instance_id,
        "action":        job.action.value,
        "started":       job.started,
        "completed":     job.completed,
        "status":        job.status.value,
        "revert_status": job.revert_status.value,
    }

    # Replace existing entry if present, otherwise append
    replaced = False
    for i, j in enumerate(jobs):
        if j.get("job_id") == job.job_id:
            jobs[i] = summary
            replaced = True
            break
    if not replaced:
        jobs.append(summary)

    _atomic_write(index_path, {"jobs": jobs})
