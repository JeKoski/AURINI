# aurini.core — framework internals
#
# Public surface of the core package. Import from here rather than from
# individual modules — this is the stable interface the rest of the codebase
# (plugins, GUI, CLI) depends on.
#
# Plugin contract (blast radius boundary — changes touch every plugin):
from aurini.core.base import AuriniPlugin, CheckResult, RemedyResult
#
# Cross-OS path resolution:
from aurini.core.paths import (
    OsKey,
    aurini_data_dir,
    aurini_instances_dir,
    aurini_logs_dir,
    aurini_runtime_dir,
    configured_os_keys,
    current_os,
    instance_metadata_dir,
    is_configured_for_current_os,
    make_path_record,
    missing_os_keys,
    resolve_path,
    resolve_path_strict,
    set_path,
)
#
# Action log and reversion state machine:
from aurini.core.log import (
    JobAction,
    JobLog,
    JobRevertStatus,
    JobStatus,
    LogEntry,
    RevertStatus,
    RevertType,
    find_incomplete_reversions,
    load_index,
)
#
# Instance management:
from aurini.core.instance import Instance, PathMode
#
# Profile management:
from aurini.core.profile import Profile
#
# Core runner — drives check → remedy → summary → execute flow:
from aurini.core.runner import Runner, RunnerPhase, RunnerSummary
