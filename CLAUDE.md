# AURINI — Project Context for Claude

## What is AURINI?

AURINI is a standalone open source desktop application for installing, configuring, updating, and managing local AI components (llama.cpp, Kokoro, Whisper, etc.) — designed for non-technical end users who currently have no good way to get these tools running without spending days in the terminal.

**Name origin:** AURINI is a Finnish/Karelian name — "my Auri" (possessive form). "Auri" means dawn in Finnish, which maps naturally onto the tool's purpose: it clears the path so everything else can begin. The name is a sibling to SENNI (the companion AI project this grew out of), which follows the same philosophy of using real Finnish names with meaningful backronyms.

**Backronym:** Automated Unified Runtime Interface for Neural Intelligence

**Sister project:** [SENNI](https://github.com/sdesser/senni) — a local AI companion app that uses llama.cpp as its inference backend. SENNI will have its own AURINI plugin.

---

## The Problem AURINI Solves

Getting llama.cpp (or any local AI tool) running requires:
- Knowing your GPU vendor and what backend to use
- Installing the right toolkits (oneAPI, CUDA, ROCm, etc.)
- Building from source with the correct cmake flags for your hardware
- Managing model files, launch arguments, context sizes, quantisation
- Knowing what to do when something breaks

This is too much to expect from regular users. The Discord communities around local AI are full of people getting stuck at each of these steps. AURINI's goal is to make this a guided, GUI-driven experience — the user tells it what they have, it figures out the rest.

---

## Design Philosophy

- **Non-technical users are the primary citizen**, not an afterthought
- **Transparent and informative** — always show the user what was detected, what is about to happen, and why. Never do things silently
- **Nothing happens until the user confirms** — collect all info, show a summary, then ask before touching the system
- **Pre-flight checks are the product** — the install flow does not start until every dependency and precondition has been verified. If something is missing, AURINI does not error out — it tells the user exactly what's missing and either fixes it automatically or gives a single clear instruction
- **Raw output is never discarded** — every command AURINI runs captures full stdout/stderr regardless of outcome. The user can always view raw output from the GUI. Critical for unexpected failures, version string changes, and anything AURINI doesn't recognise
- **Raw flags are always visible** — plain English labels are the primary way to understand a setting, but the underlying flag is always shown (secondary styling). Users need to connect settings to error messages, search documentation, and understand what AURINI is actually passing to the process. Hiding flags makes debugging unnecessarily hard in a space where tools change rapidly
- **Everything AURINI does is reversible** — backups before any destructive action, an action log the user can inspect, clear undo instructions for every fix applied
- **Safe by default** — back up before modifying, never delete without explicit instruction, always preserve custom files
- **Guided, not just documented** — don't just error out, explain what went wrong and what to do next
- **Modular** — new components (Kokoro, Whisper, etc.) are self-contained plugins; the core tool provides the framework
- **Instances are a power feature, not a UI burden** — the default path (one instance, created automatically) should feel invisible. Instance management surfaces only when the user needs it
- **We don't assume anything about the user's system** — check everything that's needed, automate what we can, and guide the user through what we can't. Every dependency is explicitly verified, every remedy has a fallback, and manual steps always include clear instructions so the user can complete them and continue the automated flow.

### Auto-fix policy

When a check fails and AURINI can fix it:

| Risk level | Behaviour |
|---|---|
| Low risk (e.g. creating a directory) | Auto-fix by default. Report what was done and how to undo it |
| High risk (e.g. system group membership, modifying system files) | Ask permission first. Explain the risk and how to revert. Then act |
| Manual only (AURINI cannot fix it) | Show clear instructions. Warn about consequences. Let user proceed at their own risk with explicit confirmation |

If the user chooses to proceed past a failed check, that choice is recorded in the action log.

---

## Architecture Decision: Tauri + Python

**What:** Tauri desktop app with a Python sidecar for backend logic.

**Why Tauri over Electron:**
- Solves the bootstrap problem — ships as a single installable file, no Python required to be pre-installed
- Uses the OS's built-in browser engine (not a bundled one), so app size is ~5-10MB not ~150MB
- Frontend is still HTML/CSS/JS, so skills from SENNI transfer directly
- MIT licensed — clean for open source

**Why Python for the backend:**
- All the hard logic (hardware detection, build systems, file management) is already written in Python (from SENNI's `config.py`, `server.py`, and `update_llama.py`)
- Python is widely understood by contributors
- No need to learn Rust for backend logic (Tauri's native language) — Python runs as a sidecar process

**Why not a web app (like SENNI):**
- SENNI can stay web-based because it already requires Python to run — the bootstrap problem doesn't apply
- AURINI's entire purpose is to work *before* anything is installed, so it can't depend on Python being present

**Why not Python desktop GUI (tkinter/PyQt):**
- PyQt is GPL licensed which would restrict the project
- These frameworks are hard to make look good
- Less flexible than web tech for building a polished UI

---

## Platform Targets

- **Linux** (primary, Ubuntu 22.04+)
- **Windows** (secondary, important for adoption)
- **macOS** (future — lower priority)

### Test hardware matrix

| Machine | OS | GPU | Notes |
|---|---|---|---|
| Current dev machine | Ubuntu + Windows 10 | Intel Arc A750 (8GB VRAM) | Primary test target, SYCL path |
| Upcoming new build | Windows 11 | RTX 5060 Ti | CUDA path |
| Upcoming new build | Windows 11 | Intel Core Ultra 270K iGPU | Integrated graphics / SYCL |

This gives good coverage of the most common local AI setups: Intel Arc SYCL on both Linux and Windows, NVIDIA CUDA, and Intel integrated graphics.

---

## Managed Python Runtime

AURINI ships with a bundled Python 3.12.x and manages its own Python installation independently of anything on the user's system.

### How it works

- AURINI bundles a pinned Python version (e.g. 3.12.9) inside the app — first launch always works, no internet required for initial setup
- On first launch, AURINI installs this bundled version into its managed location (`~/aurini/runtime/python/`) as the active runtime
- The bundled version is a guaranteed floor — once the managed runtime is installed, the bundle is no longer used day-to-day
- All plugins use the AURINI-managed Python by default. Venvs provide package isolation between plugin instances

### Version management

- Python version is never updated automatically — always user-initiated
- Settings → Components shows a passive status line:
  `Python 3.12.9 (AURINI managed) | Latest stable: 3.12.11`
- User can choose to update, install a specific version, or do nothing
- Before any update, AURINI shows a compatibility warning:
  "Updating Python may affect plugins that depend on specific package versions. AURINI will rebuild all managed venvs after updating. This may take a few minutes."
- Specific version installs are supported — useful for pinning, rollback, or compatibility with a known-good state
- Every Python version change is recorded in the action log with rollback instructions

### Version conflicts

If a plugin declares a Python version requirement that conflicts with the AURINI runtime, AURINI surfaces a clear warning and lets the user proceed at their own risk. Per-instance Python versions are a future feature — not built upfront as the conflict case is rare in the local AI tooling space (everything targets 3.10+).

**Why 3.12.x:** Current stable, widely compatible with all known local AI tooling. Ship with a specific patch version so behaviour is consistent across all AURINI installations.

---

## Current Repository Structure

```
aurini/
  __init__.py
  core/
    __init__.py
    base.py       <- AuriniPlugin ABC, CheckResult, RemedyResult
    checks.py     <- Core check library (20+ check functions)
    instance.py   <- Instance CRUD, path resolution, build settings
    log.py        <- Action log and reversion state machine
    paths.py      <- Cross-OS path resolution, AURINI data dirs
    profile.py    <- Profile CRUD, launch settings, custom args
    runner.py     <- Core runner: check -> remedy -> summary -> execute flow
plugins/
  __init__.py
  llama-cpp/
    __init__.py
    plugin.json   <- Manifest: checks, remedies, settings, cmake flags
    plugin.py     <- Thin dispatcher — detects platform/GPU, selects backend
    backends/
      __init__.py
      base.py          <- LlamaCppBackend ABC
      shared.py        <- git, backup, build helpers shared by all backends
      sycl_linux.py    <- Intel Arc (SYCL), Linux — fully implemented
      sycl_windows.py  <- Intel Arc (SYCL), Windows — implemented, testing in progress
run_aurini.py     <- End-to-end install/test script
update_llama.py   <- Original prototype — superseded, can be retired eventually
```

---

## Plugin Architecture

Each component (llama.cpp, Kokoro, Whisper, etc.) is a self-contained plugin living in its own subdirectory under `/plugins/`.

### Plugin vs Instance

**Plugin** — a template. Defines what a component is, what it needs, what settings it has, and how to install it. Lives in `/plugins/`. Does not represent an actual installation.

**Instance** — a concrete installation of a plugin. Has its own install path, build configuration, profiles, and per-OS paths. A user can have multiple instances of the same plugin (e.g. one llama.cpp build for Arc, one for RTX). Lives in AURINI's data directory.

The default path (user installs a plugin once) creates one instance automatically and hides the concept entirely. Instance management surfaces only when the user creates a second one.

### Plugin subdirectory layout

```
plugins/
  llama-cpp/
    plugin.json
    plugin.py
    backends/        <- one file per GPU vendor + OS combination
      base.py
      shared.py
      sycl_linux.py
      sycl_windows.py
  kokoro/            <- Kokoro TTS plugin (session 5)
    __init__.py
    plugin.json
    plugin.py
    plugin.json
    plugin.py
```

### Backend pattern (llama-cpp and any compiled plugin)

The llama-cpp plugin uses a backend dispatcher pattern rather than a single monolithic `plugin.py`. This is required because a single file handling Intel/NVIDIA/AMD across Linux/Windows/macOS would become unreadable fast — nested `if platform == ... and vendor == ...` branches in every method.

**How it works:**
- `plugin.py` is a thin dispatcher. It detects the OS and GPU vendor, selects the right backend, and delegates everything hardware-specific to it.
- Each backend file (`sycl_linux.py`, `cuda_linux.py`, etc.) handles one platform+GPU combination and subclasses `LlamaCppBackend`.
- `shared.py` contains everything that is identical across all backends: git clone/pull, backup, cmake invocation, binary verification.
- Adding a new backend = one new file + one line in `_select_backend()`. Nothing else changes.

**Registering a new backend:**
1. Create `backends/<n>.py` subclassing `LlamaCppBackend` from `backends/base.py`
2. Implement all abstract methods: `backend_id`, `display_name`, `get_checks()`, `run_check()`, `run_remedy()`, `cmake_flags()`, `env_setup_script()`, `build_launch_env()`, `binary_path()`
3. Add an `elif` branch in `_select_backend()` in `plugin.py`
4. Done — `plugin.py`, `shared.py`, and existing backends are untouched

**Planned backends:**
| Backend file | GPU | OS | Status |
|---|---|---|---|
| `sycl_linux.py` | Intel Arc / iGPU | Linux | Done |
| `sycl_windows.py` | Intel Arc / iGPU | Windows | Done (testing in progress) |
| `cuda_linux.py` | NVIDIA | Linux | Planned |
| `cuda_windows.py` | NVIDIA | Windows | Planned |
| `rocm_linux.py` | AMD | Linux | Planned |
| `cpu.py` | CPU fallback | All | Planned |

### plugin.json — manifest schema

The manifest is read by the GUI without executing any Python. It contains everything needed to display the plugin, its dependencies, its checks, its settings, and what it exposes to other plugins and apps.

See `plugins/llama-cpp/plugin.json` for the full working example. Key sections:

```json
{
  "id": "llama-cpp",
  "platforms": ["linux"],
  "checks": [...],
  "remedies": [...],
  "settings": [...],
  "build_config": {
    "cmake_flags": [...],
    "oneapi_setvars": "/opt/intel/oneapi/setvars.sh",
    "binary_path": "build/bin/llama-server"
  },
  "exposes": {
    "binary_path": "build/bin/llama-server",
    "http_service": { "default_port": 8080, "health_endpoint": "/health" }
  }
}
```

### Settings phases

| Phase | Shown to user as | Meaning | Behaviour on change |
|---|---|---|---|
| `build` | "Requires rebuild" | Compile-time cmake flags | Warn user, offer to schedule rebuild |
| `launch` | "Applied at launch" | Passed to process at startup | Takes effect next launch |
| `config` | "Saved setting" | Persistent config, not a launch arg | Saved immediately |

### Settings types

| Type | Input widget |
|---|---|
| `boolean` | Toggle |
| `integer` | Number input with min/max |
| `float` | Number input with min/max/step |
| `string` | Text input |
| `path` | Path picker |
| `enum` | Dropdown |

### Settings display

Every setting shows:
- **Label** — plain English, primary display
- **Flag** — the raw flag name, always visible in secondary styling
- **Description** — plain English explanation of what it does and when to use it
- **Enabled toggle** — every setting (preset or custom) can be disabled without being deleted. Disabled settings are shown but not passed to the process. This handles deprecated flags, renamed flags, compatibility issues, and format changes without losing the user's configured value
- **Value** — the input widget appropriate to the type

### Custom arguments

Every plugin instance has a custom arguments section below the presets. Users can add arbitrary key/value pairs (or bare flags) that are passed through as-is. AURINI does not validate custom args — it passes them along and records their use in the action log. Custom args follow the same enabled/disabled toggle pattern as presets.

### plugin.py — interface contract

Every plugin module must implement the `AuriniPlugin` base class from `aurini/core/base.py`.

```python
from aurini.core.base import AuriniPlugin, CheckResult, RemedyResult

class Plugin(AuriniPlugin):
    def get_checks(self) -> list[str]: ...
    def run_check(self, check_id: str) -> CheckResult: ...
    def run_remedy(self, remedy_id: str) -> RemedyResult: ...
    def install(self, config: dict) -> None: ...
    def update(self, config: dict) -> None: ...
    def uninstall(self) -> None: ...
    def build_launch_command(self, profile: dict) -> list[str]: ...
```

**Important:** Plugins with multiple backends should also expose:
- `set_job_log(job_log)` — inject the JobLog before any system-modifying call
- `set_install_path(path)` — update install path after construction
- `build_launch_env(base_env)` — return hardware-specific environment dict for launch

### CheckResult interface

```python
@dataclass
class CheckResult:
    check_id: str
    passed: bool
    message: str          # Human-readable interpreted result
    raw_output: str       # Full raw command output — never discarded
    remedy_id: str | None
    risk: str | None      # "low" | "high" | "manual"
    metadata: dict        # Optional structured data for plugin-internal use
```

### RemedyResult interface

```python
@dataclass
class RemedyResult:
    remedy_id: str
    success: bool
    message: str
    undo_instructions: str  # Always required — never nullable
    raw_output: str
```

---

## Action Log

Every action AURINI takes against the system is recorded in `aurini/core/log.py`.

### Key design decisions

**Log per job, not a global append-only log.** Each install/update/uninstall is a `JobLog` with its own file. When all entries in a job are reverted, that job is fully undone. A global index (`index.json`) lists all jobs for the GUI. Drilling into a job shows the full entry list.

**Reversion is a state machine.** Jobs and individual entries both track reversion state:
- Job: `not_started` → `in_progress` → `awaiting_user` → `complete` / `partially_reverted`
- Entry: `pending` → `in_progress` → `awaiting_user` → `complete` / `failed` / `skipped`

**Reversion always walks entries in reverse order.** Entry IDs are immutable and never renumbered. Skipped entries stay in the list marked as skipped — they are never removed.

**`awaiting_user` enables pause/resume.** When a manual step requires the user to act (e.g. logout/login after group change), reversion pauses with `awaiting_user`. On next AURINI startup, `find_incomplete_reversions()` detects these and surfaces them immediately.

**Atomic writes.** Every state change writes via temp file + rename. A crash mid-write leaves the previous file intact rather than producing corrupt JSON.

**Directory layout:**
```
~/aurini/logs/
  index.json
  jobs/
    2026-04-01_14-32-07_llama-cpp-arc_install.json
    2026-04-01_15-10-22_llama-cpp-arc_update.json
```

### Revert types

| Type | Meaning |
|---|---|
| `AUTO` | AURINI can run the revert command — still requires explicit user confirmation |
| `MANUAL` | AURINI cannot run it — surfaces instructions, waits for user to confirm done |

`AUTO` means AURINI *can* run the reversal, not that it does so silently. Confirmation is always required.

---

## Cross-OS Path Handling (`aurini/core/paths.py`)

Each instance stores paths per OS in a dict: `{"linux": "~/llama.cpp", "windows": "C:/llama.cpp", "macos": null}`.

Key functions:
- `current_os()` → `OsKey` (LINUX / WINDOWS / MACOS)
- `resolve_path(paths)` → `Path | None` — expands `~`, returns None if not configured
- `resolve_path_strict(paths, instance_id)` → `Path` — raises with clear message if not configured
- `set_path(paths, value)` → new dict (immutable — never mutates input)
- `make_path_record(linux, windows, macos)` → fresh per-OS dict
- `configured_os_keys(paths)` / `missing_os_keys(paths)` — for UI display

**`~` is preserved in stored strings** and only expanded at resolve time. This keeps records portable across user accounts and machines.

**AURINI data directories** (from `paths.py`):
- Linux/macOS: `~/aurini/`
- Windows: `%APPDATA%/aurini/`

Subdirectories: `logs/`, `instances/`, `runtime/python/`

---

## Core Check Library (`aurini/core/checks.py`)

20+ ready-to-use check functions. All return `CheckResult`, never raise.

**System identity:** `os_is`, `arch_is`
**Hardware:** `gpu_vendor_is`, `gpu_visible`
**User permissions:** `user_in_group`, `directory_writable`, `directory_readable`, `can_run_without_sudo`
**Software presence:** `file_exists`, `command_exists`, `command_succeeds`, `command_output_contains`, `version_gte`, `python_package_installed`, `process_running`
**Network and disk:** `internet_reachable`, `host_reachable`, `disk_space_gte`
**Runtime state:** `port_in_use`, `lock_file_present`

`gpu_visible` accepts `env_setup_command` for cases where the toolkit must be sourced first (e.g. oneAPI before sycl-ls). `version_gte` stores the detected version string in `metadata` so later steps can read it without re-running the command. All string matching is case-insensitive.

**Important:** `import grp` at the top of `checks.py` is wrapped in a try/except because `grp` is a Unix-only module. The `user_in_group` function guards against `grp is None` and returns a clear failure message on Windows. This pattern must be preserved — do not revert to an unconditional `import grp`.

---

## Instance Model

A **plugin** is a template. An **instance** is a concrete installation.

Each instance has:
- A user-given name (e.g. "llama.cpp — Arc", "llama.cpp — RTX")
- Its own install path (per OS, via `paths.py`)
- Its own build-phase settings (compile flags)
- Its own set of profiles (launch-phase settings)
- Its own managed venv (for Python-based plugins)
- AURINI metadata directory at `~/aurini/instances/<instance_id>/`

The install path can be AURINI managed (default) or custom (user specifies a path, including existing installations). AURINI's metadata for the instance always lives in the AURINI managed path regardless of where the install itself is.

### Instance data structure

```json
{
  "instance_id": "llama-cpp-arc",
  "plugin_id": "llama-cpp",
  "display_name": "llama.cpp — Arc",
  "created": "2026-04-01T14:32:07Z",
  "path_mode": "custom",
  "paths": {
    "linux": "~/llama.cpp/",
    "windows": "C:/llama.cpp/",
    "macos": null
  },
  "aurini_managed_paths": {
    "linux": "~/aurini/instances/llama-cpp-arc/",
    "windows": "C:/aurini/instances/llama-cpp-arc/"
  },
  "build_settings": {
    "fp16": { "enabled": true, "value": true },
    "flash_attn_build": { "enabled": false, "value": false }
  },
  "active_profile": "gemma-27b-quality",
  "profiles": []
}
```

### Inter-plugin references

Plugins reference each other by instance ID, not by path. AURINI resolves paths at runtime via `paths.py`.

### Known instance model limitations (design for these later)

- **External installs** — import existing installation flow needed
- **Profile portability** — copy compatible settings, flag incompatible ones between instances
- **Simple user path** — one instance created automatically, management hidden until second is created
- **Custom path adoption** — detect existing files, back up before touching, offer adopt vs reinstall

---

## Profiles

A profile is a named snapshot of all `launch`-phase settings for a plugin instance.

### Profile data structure

```json
{
  "profile_id": "gemma-27b-quality",
  "display_name": "Gemma 27B — High Quality",
  "notes": "Best quality, needs full 8GB VRAM free",
  "created": "2026-04-01T14:32:07Z",
  "is_default": true,
  "settings": {
    "model_path": { "enabled": true, "value": "~/models/gemma-27b-q4.gguf" },
    "ctx_size":   { "enabled": true, "value": 8192 },
    "flash_attn": { "enabled": true, "value": true },
    "gpu_layers": { "enabled": true, "value": 99 }
  },
  "custom_args": [
    { "flag": "--threads", "value": "6", "enabled": true }
  ]
}
```

**The model path lives in the profile, not the instance.** This is what makes switching between Gemma and Qwen practical — the entire launch configuration is captured in the profile.

---

## Install Flow State Machine

```
DETECT
  └─► CONFIGURE (user sets build-phase options, names the instance)
        └─► PRE-FLIGHT CHECKS (all checks run, results collected)
              └─► SUMMARY SCREEN (what we found, what's ready, what needs attention)
                    └─► [user confirms]
                          └─► EXECUTE (install steps run)
                                ├─► VERIFY (check install succeeded)
                                │     └─► DONE
                                └─► ERROR
                                      └─► RECOVER (preserve backups, explain failure, suggest fixes)
```

Nothing touches the system until the user presses "Begin installation" on the summary screen.

---

## Hardware Detection

Detected at startup, presented to user for confirmation before doing anything:

| GPU Vendor | Backend | Required Toolkit |
|---|---|---|
| Intel Arc / iGPU | SYCL | Intel oneAPI |
| NVIDIA | CUDA | CUDA Toolkit |
| AMD | ROCm | ROCm |
| Apple Silicon | Metal | Built into macOS |
| None / unknown | CPU | Nothing extra |

Detection methods:
- Linux: `lspci` output — checks VGA, 3D controller, display controller lines. NVIDIA and AMD checked before Intel to correctly identify discrete GPUs on systems with Intel integrated graphics.
- Windows: `wmic path win32_VideoController get name` — checks for NVIDIA and AMD before Intel; Intel check requires "arc", "iris", or "uhd" in the adapter name to avoid false matches on Intel chipsets without SYCL-capable GPUs. Result is cached via function attribute to avoid repeated slow wmic calls. **Note:** wmic is deprecated from Windows 10 21H1+ and may be removed in a future Windows release. If detection stops working, replace with a PowerShell `Get-WmiObject Win32_VideoController` query.
- macOS: `system_profiler` (planned)

---

## Key Design Decisions & Reasoning

### Summary screen before any action
Every flow must show a "here's what we found, here's what will happen" screen before touching the system. Non-technical users are stressed by installers that do things silently. A clear summary with a single confirmation button builds trust.

### Raw output always captured and visible
Every command AURINI runs captures full stdout/stderr regardless of outcome. The user can always view it from the GUI. Non-negotiable — it's what makes debugging possible when something breaks unexpectedly, including cases where tool output format changes (e.g. if Intel changes `level_zero:gpu` to something else in a future update).

### Raw flags always visible in the UI
Plain English labels are primary, raw flags are secondary — but always shown. Users need to connect settings to error messages and documentation. Hiding flags makes debugging unnecessarily hard in a space where tools change rapidly and flag names appear directly in error output.

### Argument enabled/disabled toggle
Every setting (preset or custom) has an enabled/disabled toggle independent of its value. Disabled settings are shown but not passed to the process. This handles deprecated flags, renamed flags, compatibility issues, and format changes without losing the user's configured value. Users can disable a broken preset and add the corrected flag as a custom argument while keeping both visible for reference.

### Profiles own the model path
The model path is a per-profile setting, not a per-instance setting. This is what makes switching between different models (Gemma, Qwen, etc.) practical — the entire launch configuration including the model is captured in the profile, and switching profiles is instant.

### Backend dispatcher pattern
A single plugin.py handling all GPU vendors and OSes would become unreadable fast. The dispatcher selects a backend based on detected platform+GPU. Each backend handles one combination. shared.py contains everything identical across backends. Adding a new backend = one new file + one line in the dispatcher. See "Backend pattern" section above.

### Backup modified tracked files, not just untracked
Git's `status --porcelain` returns both untracked (`??`) and locally modified tracked files (`M`). Early versions of `update_llama.py` only backed up untracked files — this caused a real failure when `examples/sycl/build.sh` (a repo file the developer had modified) blocked `git pull`. Both types must be backed up before any git operation, then modified tracked files reset with `git checkout .`.

### Timestamp on backup folders
`llama.cpp_backup_2026-04-01_14-32-07` not `llama.cpp_backup_2026-04-01`. Running the installer twice in a day would silently overwrite the first backup otherwise.

### Log per job with state machine reversion
A global append-only log makes undo ordering complex. Per-job files with a reversion state machine make it clear: when all entries in a job are reverted, that job is fully undone. Reversion can pause (`awaiting_user`) for manual steps (e.g. logout/login) and resume on next AURINI startup. Entries are never renumbered or removed — skipped entries stay in the list marked skipped.

### FP16 default ON
Most users running local models have consumer cards with limited VRAM (8-12GB). FP16 halves compute buffer VRAM usage with negligible quality impact. Defaulting it off would be wrong for the majority of users.

### Explicit compiler flags in cmake
`-DCMAKE_C_COMPILER=icx -DCMAKE_CXX_COMPILER=icpx` must be passed explicitly even after sourcing setvars.sh. Without them cmake may fall back to system gcc/g++ and produce a broken SYCL build silently.

### sycl-ls verification before building
A 20-30 minute build that was always going to fail due to GPU not being visible is a terrible user experience. Run `sycl-ls` first, check for `level_zero:gpu` in the output (not just exit code), warn if not found, give the user a chance to cancel.

### Intel Deep Learning Essentials over full Base Toolkit
The official llama.cpp SYCL docs recommend this lighter package for llama.cpp specifically. It has everything needed and is significantly smaller. Always present it as the recommended option.

### render/video group membership check
Intel GPU access on Linux requires the user to be in these groups. Check early, warn clearly with the exact fix command (`sudo usermod -aG render,video $USER`), explain that a logout/login is required. Don't block the build (affects runtime not build time) but make sure the user knows.

### Never require sudo to build
If the build folder has wrong ownership (e.g. created by a previous sudo run), cmake will fail with Permission Denied. Detect this and suggest `sudo chown -R $USER:$USER ~/llama.cpp/build` rather than leaving the user confused.

### Plugin interface is the blast radius boundary
Any change to the plugin contract (`CheckResult`, `RemedyResult`, `AuriniPlugin` base class) potentially touches every plugin. These changes must be flagged explicitly, treated as requiring extra care, and documented with reasoning in this file.

### Shared Python, venvs for isolation
One AURINI-managed Python runtime shared across all plugins. Venvs provide package isolation per instance. Per-instance Python versions are a future feature — not built upfront as version conflicts are rare in the local AI tooling space.

### Hyphenated plugin folder names and importlib
Plugin folders use hyphenated names (`llama-cpp`) for readability and consistency with plugin IDs. Python cannot import hyphenated names with a standard `import` statement. The core must load plugins via `importlib.util.spec_from_file_location()`. This is the correct pattern anyway since plugins are discovered dynamically at runtime — hyphenation just makes it explicit that plugins are not regular Python packages. Test harnesses must also use this pattern (register module stubs with `sys.modules` before loading).

### Windows oneAPI environment activation
On Linux, `source setvars.sh` modifies the current shell environment. On Windows, `setvars.bat` only modifies the cmd.exe child process it runs in. AURINI captures the post-activation environment by writing a temporary batch file that calls `setvars.bat intel64 --force`, prints a unique sentinel line (`AURINI_ENV_BEGIN`), then runs `set` to dump all env vars. Only lines after the sentinel are parsed — this sidesteps setvars.bat's banner output (which goes to stdout, not stderr) without relying on fragile prefix-based filtering. The env capture is done in `_capture_setvars_env()` in `sycl_windows.py` and is reused for sycl-ls checks. **Do not strip values when parsing `set` output** — env var values are exact and stripping can corrupt PATH entries.

**Critical: use a batch file for the build, not `&&` chaining.** setvars.bat internally calls vcvarsall.bat (to set up cl.exe and the Windows SDK) via `call`. The `call` command propagates environment changes to the calling process. The `&&` operator does NOT — each step in a `cmd /c "A && B"` chain runs in the same process but env changes made by batch files in step A (including those made via internal `call`) are NOT visible to step B. This means cl.exe and kernel32.lib end up missing from cmake even though setvars.bat appeared to run successfully. The fix: write a temp .bat file with `call setvars.bat` followed by the cmake commands. All steps share the same process and env changes propagate correctly. This is why `run_build_windows()` in `shared.py` uses a temp batch file rather than `&&` chaining, while `_run_in_setvars_env()` (used only for sycl-ls checks) can use `&&` safely because it only needs the PATH populated, not the full MSVC environment.

### winget availability is not assumed on Windows
winget ships with Windows 10 1709+ via App Installer, but may be absent on some machines (older installs, LTSC editions, stripped enterprise images). AURINI checks for winget first and caches the result. Remedies that would use winget degrade gracefully to manual instructions with download URLs if it is not available. This is the correct "check everything, guide if we can't automate" pattern for Windows.

### VS detection via vswhere, not PATH
Visual Studio is not on PATH by default — it must be located via `vswhere.exe` (ships with VS 2017+, lives at a fixed path in Program Files). AURINI uses vswhere with a version range filter `[17,)` (VS2022 is 17.x, VS2026 is 18.x — open-ended to handle future versions) and requires the `Microsoft.VisualCpp.Tools.HostX64.TargetX64` component to confirm the C++ workload is installed, not just the IDE shell. Falls back to scanning known install paths if vswhere itself is absent.

### Windows SDK is not included in the VS C++ workload
The Windows SDK (which provides rc.exe, mt.exe, kernel32.lib, ucrt, etc.) is a separate component that must be explicitly ticked in the VS Installer under Individual Components → SDKs. It is NOT installed by default with the "Desktop development with C++" workload. Without it, cmake compiler detection fails with `LNK1104: cannot open file 'kernel32.lib'` and `rc: no such file or directory`. AURINI checks for this explicitly via `windows_sdk_present` (looks for rc.exe in `Windows Kits\10\bin\<version>\x64\`) and injects the SDK bin dir into the build PATH.

### Visual Studio install directory naming
VS2022 installs to `\Microsoft Visual Studio\2022\`. VS2026 installs to `\Microsoft Visual Studio\18\` (using its internal version number, not the year). The `_find_cmake_windows()` and `_detect_vs2022()` helpers must check both patterns. Future VS versions may follow either convention — when adding support, check the actual install path first.

### cmake on Windows is not on PATH by default
cmake installed via the Visual Studio Installer ends up in a VS-internal path that is not added to the system PATH. `shutil.which("cmake")` will not find it. The `_find_cmake_windows()` helper in `sycl_windows.py` searches known VS-internal cmake locations before falling back to the winget remedy. The cmake directory found this way must also be injected into the subprocess PATH when running cmake during the build. Same applies to ninja (`_find_ninja_windows()`) and the Windows SDK bin dir (`_find_windows_sdk()`).

### Windows build must use a batch file, not && chaining
`run_build_windows()` in `shared.py` writes a temp `.bat` file rather than using `cmd /c "A && B && C"`. This is critical: setvars.bat internally calls vcvarsall.bat via `call`, which sets up cl.exe and the Windows SDK paths. The `call` command propagates env changes within the same batch file process. The `&&` operator does NOT — env changes made by batch files in one `&&` step are not visible to the next step. Using `&&` chaining causes cl.exe and kernel32.lib to be missing from cmake even though setvars.bat appears to run successfully.

### Windows binary path differs from Linux
Linux: `build/bin/llama-server`
Windows: `build\bin\llama-server.exe`
The backend's `binary_path()` method handles this — the core never hardcodes the binary location.

### SYCL_CACHE_PERSISTENT on Windows
Set in `build_launch_env()` for the Windows backend. Compiled SYCL kernels are cached between runs, giving a significant speedup on second and subsequent launches. Safe to always enable — stale cache issues are rare and can be cleared by deleting the cache dir if they occur.

### OpenCL/Vulkan Compatibility Pack gotcha
Installing Intel Arc drivers on Windows sometimes silently installs the Microsoft OpenCL/Vulkan Compatibility Pack from the Microsoft Store. This package can block sycl-ls from finding level_zero GPU devices. It appears in the Microsoft Store and can be uninstalled from there. Surfaced in the `remedy_gpu_not_visible` instructions so users have a clear path forward.

### cmd emoji rendering on Windows
Unicode emoji (✓, ✗, etc.) do not render correctly in Windows cmd.exe — they display as `[?]`. run_aurini.py needs an ASCII fallback for Windows: `[OK]` / `[FAIL]` / `[??]` etc. Detect via `sys.platform == "win32"` and choose symbol set accordingly.

---

## Critical Working Rules

- **Always provide complete files** — never code sections, never snippets, never "find X and replace with Y". The user has ADHD and finds partial edits extremely difficult. Full file replacements only.
- **One file at a time** where possible. Flag upfront if a feature will require touching multiple files and get agreement before proceeding.
- **Stop and check in** if things start going wrong rather than pushing through. Escalating complexity when stuck makes things worse.
- **Never ask the user to remember to do things** at specific times — ADHD means this won't work. Automate it or build it into existing flows instead.
- **Suggest Extended Thinking and/or Opus** when the architecture is genuinely uncertain or a wrong call would cause cascading problems. For most feature work, standard Sonnet is fine.
- **Plugin interface is the blast radius boundary** — flag any change to the plugin contract explicitly before proceeding.
- **Backend dispatcher pattern is the blast radius boundary for backends** — flag any change to `LlamaCppBackend` ABC explicitly before proceeding.
- **Keep Python files single-responsibility and small.** One file per plugin, one file per major core concern. If a file is getting long, that's a signal to split before it becomes a problem.
- **Architecture decisions go in CLAUDE.md with reasoning, not just the decision.** Future sessions won't have this conversation's context — the *why* is as important as the *what*.
- **End every session by updating CLAUDE.md and any relevant design docs.** This is non-negotiable — it's what makes the next session productive.
- **Remind user to push changes** at the end of every session.

---

## TODO / Known Issues

### Done — session 1
- [x] `aurini/core/base.py` — AuriniPlugin ABC, CheckResult, RemedyResult
- [x] `aurini/core/checks.py` — core check library (20+ functions)
- [x] `aurini/core/log.py` — action log, per-job files, reversion state machine
- [x] `aurini/core/paths.py` — cross-OS path resolution, AURINI data dirs
- [x] `plugins/llama-cpp/plugin.json` — full manifest
- [x] `plugins/llama-cpp/plugin.py` — thin dispatcher with backend detection
- [x] `plugins/llama-cpp/backends/base.py` — LlamaCppBackend ABC
- [x] `plugins/llama-cpp/backends/shared.py` — git, backup, build helpers
- [x] `plugins/llama-cpp/backends/sycl_linux.py` — Intel Arc, Linux

### Done — session 2
- [x] `aurini/core/runner.py` — core runner: check → remedy → summary → execute flow, reversion, ties log.py in
- [x] `aurini/core/instance.py` — instance CRUD, path resolution, build settings, managed vs custom paths
- [x] `aurini/core/profile.py` — profile CRUD, launch settings, custom args, default management
- [x] `aurini/core/__init__.py` — clean public exports for all core modules

### Done — session 3
- [x] `run_aurini.py` — end-to-end install script; first real hardware test on Arc A750, confirmed working on Linux
- [x] `aurini/core/checks.py` — fixed disk_space_gte to walk up to nearest existing ancestor when install path doesn't exist yet; wrapped `import grp` in try/except for Windows compatibility; added guard to `user_in_group` for Windows
- [x] `aurini/core/runner.py` — fixed ghost remedy jobs: only create remedy JobLog when remedies are actually applied; call `mark_complete()`/`mark_failed()` at end of `run_remedies()`
- [x] `plugins/llama-cpp/backends/sycl_windows.py` — Intel Arc, Windows 10/11; full check + remedy suite including winget detection, VS2022/2026 detection via vswhere, oneAPI env capture, Arc driver version check, cmake/ninja/git install
- [x] `plugins/llama-cpp/plugin.py` — Windows backend wired into dispatcher; added `_detect_gpu_vendor_windows()` with caching and wmic deprecation note

### Done — session 4
- [x] **Fix `_capture_setvars_env()` in `sycl_windows.py`** — env capture was returning 103 keys but PATH was missing, causing sycl-ls to fail. Root cause: setvars.bat banner output goes to stdout (not stderr), and prefix-based filtering (`line.startswith(":")`) didn't catch all banner lines. Fix: print a sentinel (`AURINI_ENV_BEGIN`) after `call setvars.bat` and before `set`, then only parse lines after the sentinel. Also removed `value.strip()` — env var values are exact and stripping can corrupt PATH entries. Also fixed `key.upper()` — Windows env vars are case-insensitive but `set` outputs `Path` not `PATH` on some machines.
- [x] **Fix `_run_in_setvars_env()` in `sycl_windows.py`** — rewritten to use SENNI-style `&&` shell chaining (`setvars.bat && sycl-ls`) instead of env capture. Simpler and reliable for running checks. Env capture is still used for `build_env()`.
- [x] **Fix ninja not found** — added `_find_ninja_windows()` that searches VS-internal locations (`CommonExtensions\Microsoft\CMake\Ninja\ninja.exe`), same pattern as `_find_cmake_windows()`. Ninja ships with VS alongside cmake.
- [x] **Fix `_log_remedy()` in `sycl_windows.py`** — was calling `self._job_log.record_remedy()` which doesn't exist. Fixed to use `add_entry()` matching `sycl_linux.py`.
- [x] **Fix multiple instances being created each run** — `get_or_create_instance()` in `run_aurini.py` hardcoded `custom_paths={"linux": ...}` even on Windows. Fixed to use `current_os().value` as the key.
- [x] **Fix Windows build — bash script replaced with temp batch file** — `shared.run_build()` used a bash script (`set -e`, `shlex.quote`) which doesn't work on Windows. Added `run_build_windows()` that writes a temp `.bat` file with `call setvars.bat` followed by cmake. Critical: must use a batch file, not `&&` chaining — `call` propagates env changes from setvars.bat's internal vcvars call; `&&` does not.
- [x] **Fix cmake/ninja not on PATH during build** — `_build()` in `plugin.py` now calls `_find_cmake_windows()` and `_find_ninja_windows()` and injects their parent dirs into PATH via the batch file's `set PATH=...` line.
- [x] **Add `windows_sdk_present` check** — Windows SDK (rc.exe, kernel32.lib) is not part of the VS C++ workload and must be installed separately via the VS Installer. Added check, remedy with clear instructions, `_find_windows_sdk()` helper, and PATH injection of the SDK bin dir during build.
- [x] **Windows build confirmed reaching compilation** — all checks pass, clone succeeds, cmake configures, build running on Arc A750 / Windows 10.

### Done — session 5
- [x] **Kokoro TTS plugin** — `plugins/kokoro/plugin.py`, `plugins/kokoro/plugin.json`, `plugins/kokoro/__init__.py`. Full `AuriniPlugin` implementation. No backend dispatcher — Kokoro is pure Python, all platforms handled in a single plugin.py.
  - Checks: `python_usable`, `kokoro_importable`, `soundfile_importable`, `espeak_present`, `voices_dir_exists`
  - Remedies: `remedy_pip_kokoro` (auto), `remedy_pip_soundfile` (auto), `remedy_install_espeak` (auto — apt on Linux, winget on Windows), `remedy_voices_dir_missing` (manual — Hugging Face download, no auto-fix)
  - Install/update/uninstall via pip; espeak-ng via system package manager
  - `get_senni_config()` returns `{"python_path", "voices_path", "espeak_path"}` — the three values SENNI writes into `config.json["tts"]`
  - `build_launch_command()` raises `NotImplementedError` with a clear message — Kokoro has no standalone server process; SENNI's `tts_server.py` owns the subprocess lifecycle
  - Voices directory check reports voice file count and names up to 5; auto-discovers `voices/` next to `plugin.py` if `voices_path` is not configured

### Next session — start here
- [ ] **ASCII fallback for emoji in cmd.exe** — ✓/✗ render as [?] in Windows cmd. Detect `sys.platform == "win32"` in `run_aurini.py` and `run_launch.py`, use `[OK]`/`[FAIL]` instead. Quick fix.
- [ ] **Update flow test** — `plugin.update()` exists but hasn't been tested. Write a quick `run_update.py` or add an update mode to `run_aurini.py`. Important for real-world use.
- [ ] **GUI layer (Tauri)** — backend is ready. This is the next big thing.

### Remaining work
- [ ] `backends/cuda_linux.py` — NVIDIA, Linux
- [ ] `backends/cuda_windows.py` — NVIDIA, Windows
- [ ] `backends/rocm_linux.py` — AMD, Linux
- [ ] `backends/cpu.py` — CPU fallback, all platforms
- [ ] GUI layer (Tauri)
- [ ] SENNI plugin
- [x] Kokoro TTS plugin — **done session 5**
- [ ] Managed venv system (implement when first Python-based plugin requires it)
- [ ] Per-instance Python versions (future — only if version conflict cases emerge)
- [ ] "Import existing installation" flow for externally installed components
- [ ] Profile portability between instances
- [ ] Model file management (download, organise, delete)
- [ ] Python runtime update UI (Settings → Components)
- [ ] Retire `update_llama.py` once first end-to-end test confirms core runner works
- [ ] Data directory mode setting — AURINI currently hardcodes its data directory (`~/aurini/` on Linux/Mac, `%APPDATA%/aurini/` on Windows). Three modes should be supported: `os_default` (current behaviour), `portable` (a `aurini-data/` folder next to the executable), and `custom` (user-specified path). The mode preference is stored in a small fixed-location config file (`~/.config/aurini/aurini.conf` on Linux/Mac) that never moves regardless of which mode is chosen. Only `aurini_data_dir()` in `paths.py` needs to change; everything downstream follows automatically. As a dev convenience, `aurini_data_dir()` can check for an `AURINI_DATA_DIR` env var and use that if set. Best implemented when the GUI settings screen exists.

---

## `update_llama.py`
The original prototype of AURINI's llama.cpp plugin. Now superseded by the plugin architecture but kept for reference. The logic has been ported into `backends/sycl_linux.py` and `backends/shared.py`.

**Exact cmake flags used (Intel Arc, Linux):**
```
-DGGML_SYCL=ON
-DGGML_SYCL_TARGET=INTEL
-DGGML_SYCL_DNN=ON
-DGGML_SYCL_GRAPH=ON
-DGGML_SYCL_F16=ON  (default, user can disable)
-DCMAKE_BUILD_TYPE=Release
-DCMAKE_C_COMPILER=icx
-DCMAKE_CXX_COMPILER=icpx
```

**oneAPI path (standard install):** `/opt/intel/oneapi/setvars.sh`

**Known hardware:** Developer uses Intel Arc A750 (8GB VRAM) on Ubuntu. FP16 is important for this card — halves VRAM usage for compute buffers without affecting model weight quality.

---

## Reference: SENNI's Intel Launch Command (Linux)

From SENNI's `server.py` — this is what a working llama-server launch looks like for Intel Arc on Linux:

```python
oneapi_sh = "/opt/intel/oneapi/setvars.sh"
safe_cmd  = " ".join(shlex.quote(a) for a in cmd_args)
full_cmd  = f". {oneapi_sh} --force ; exec {safe_cmd}"
shell_args = {"shell": True, "executable": "/bin/bash"}
env["ONEAPI_DEVICE_SELECTOR"] = "level_zero:gpu"
```

The `exec` replaces the shell with llama-server so the PID is the actual target process (important for clean shutdown). AURINI implements the equivalent via `build_launch_env()` setting `ONEAPI_DEVICE_SELECTOR` and the core runner wrapping the command with setvars sourcing.

---

## Useful Links

- [llama.cpp SYCL backend docs](https://github.com/ggml-org/llama.cpp/blob/master/docs/backend/SYCL.md)
- [Intel oneAPI Base Toolkit](https://www.intel.com/content/www/us/en/developer/tools/oneapi/base-toolkit.html)
- [Tauri docs](https://tauri.app/start/)
- [SENNI repo](https://github.com/sdesser/senni) — see `scripts/config.py` for hardware detection, `scripts/server.py` for launch logic, `installation/update_llama.py` for the original llama.cpp installer prototype
