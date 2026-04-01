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
  kokoro/
    plugin.json
    plugin.py
  whisper/
    plugin.json
    plugin.py
```

### plugin.json — manifest schema

The manifest is read by the GUI without executing any Python. It contains everything needed to display the plugin, its dependencies, its checks, its settings, and what it exposes to other plugins and apps.

```json
{
  "id": "llama-cpp",
  "name": "llama.cpp",
  "version": "1.0.0",
  "description": "High-performance local LLM inference engine.",
  "author": "AURINI project",
  "platforms": ["linux", "windows"],

  "dependencies": [
    {
      "id": "oneapi",
      "name": "Intel oneAPI",
      "description": "Required for Intel Arc GPU acceleration via SYCL.",
      "required": true,
      "risk": "high",
      "auto_fixable": false,
      "platforms": ["linux", "windows"]
    }
  ],

  "checks": [
    {
      "id": "oneapi_present",
      "type": "file_exists",
      "description": "oneAPI setvars.sh present",
      "path": "/opt/intel/oneapi/setvars.sh",
      "on_fail": "remedy_install_oneapi"
    },
    {
      "id": "gpu_visible",
      "type": "command_output_contains",
      "description": "GPU visible via sycl-ls",
      "command": "sycl-ls",
      "expected_string": "level_zero:gpu",
      "on_fail": "remedy_gpu_not_visible"
    }
  ],

  "settings": [
    {
      "id": "fp16",
      "label": "FP16 compute",
      "flag": "-DGGML_SYCL_F16=ON",
      "description": "Halves VRAM used for compute buffers with negligible quality impact. Recommended for Arc A750/A770 and similar 8GB cards.",
      "type": "boolean",
      "default": true,
      "category": "performance",
      "phase": "build"
    },
    {
      "id": "flash_attn",
      "label": "Flash Attention",
      "flag": "--flash-attn",
      "description": "Reduces VRAM usage during inference. Recommended for cards with 8GB or less.",
      "type": "boolean",
      "default": false,
      "category": "memory",
      "phase": "launch"
    },
    {
      "id": "ctx_size",
      "label": "Context size",
      "flag": "--ctx-size",
      "description": "Maximum number of tokens the model can work with at once. Higher values use more VRAM.",
      "type": "integer",
      "default": 4096,
      "min": 512,
      "max": 131072,
      "category": "memory",
      "phase": "launch"
    },
    {
      "id": "model_path",
      "label": "Model",
      "flag": "--model",
      "description": "Path to the model file to load.",
      "type": "path",
      "default": null,
      "category": "model",
      "phase": "launch"
    }
  ],

  "exposes": {
    "binary_path": "bin/llama-server",
    "http_service": {
      "default_port": 8080,
      "health_endpoint": "/health"
    }
  },

  "requires_plugins": []
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

The enabled/disabled toggle is separate from the value. A user can disable `--flash-attn` without clearing the setting, then re-enable it later. This also handles the case where a flag is renamed or its format changes — the user disables the old preset and adds the corrected flag as a custom argument while keeping both visible for reference.

### Custom arguments

Every plugin instance has a custom arguments section below the presets. Users can add arbitrary key/value pairs (or bare flags) that are passed through as-is. AURINI does not validate custom args — it passes them along and records their use in the action log. Custom args follow the same enabled/disabled toggle pattern as presets.

### plugin.py — interface contract

Every plugin module must implement the `AuriniPlugin` base class provided by `aurini_core`. The core calls these methods in sequence and collects structured result objects. The core does not care how the plugin implements its logic — only that it returns the right structures.

```python
from aurini_core import AuriniPlugin, CheckResult, RemedyResult

class Plugin(AuriniPlugin):

    def get_checks(self) -> list[str]:
        """Return ordered list of check IDs this plugin wants the core to run."""
        ...

    def run_check(self, check_id: str) -> CheckResult:
        """Run a single check. Must return CheckResult regardless of outcome."""
        ...

    def run_remedy(self, remedy_id: str) -> RemedyResult:
        """Attempt a remedy. Must return RemedyResult with undo instructions."""
        ...

    def install(self, config: dict) -> None:
        """Execute the install flow. Only called after all checks pass and user confirms."""
        ...

    def uninstall(self) -> None:
        """Remove everything AURINI installed for this instance. Must be clean and complete."""
        ...

    def build_launch_command(self, profile: dict) -> list[str]:
        """Construct the launch command for a given profile. Returns argv list."""
        ...
```

### CheckResult interface

Every check — whether using a core library type or a custom plugin callable — must return a `CheckResult`:

```python
@dataclass
class CheckResult:
    check_id: str
    passed: bool
    message: str          # Human-readable interpreted result
    raw_output: str       # Full raw command output — never discarded, always shown on request
    remedy_id: str | None # Which remedy to offer if failed
    risk: str | None      # "low" | "high" | "manual"
```

`raw_output` is non-negotiable. The GUI must always offer to show it. This is what the user searches when something breaks in a way AURINI doesn't recognise — including cases where tool output format has changed.

### RemedyResult interface

```python
@dataclass
class RemedyResult:
    remedy_id: str
    success: bool
    message: str           # What was done in plain English
    undo_instructions: str # How to reverse it — always required
    raw_output: str        # Full output of any commands run
```

---

## Core Check Library

The core provides a standard library of check types that plugins can use declaratively in `plugin.json`. Plugins may also provide custom callables in `plugin.py` for anything the library doesn't cover. Both approaches are supported and can be mixed within a single plugin.

### System identity
- `os_is` — check OS type (linux, windows, macos)
- `arch_is` — check CPU architecture (x86_64, arm64)

### Hardware
- `gpu_vendor_is` — check GPU vendor (intel, nvidia, amd, apple)
- `gpu_vram_gte` — check VRAM >= N GB
- `gpu_visible` — check GPU is visible via toolkit command (sycl-ls, nvidia-smi, rocm-smi); checks output string, not just exit code

### User permissions
- `user_in_group` — check user is in a system group
- `directory_writable` — check a path is writable by the current user
- `directory_readable` — check a path is readable
- `can_run_without_sudo` — check a command runs without elevated privileges

### Software presence
- `file_exists` — check a file or directory exists at a path
- `command_exists` — check a command is on PATH
- `command_succeeds` — check a command exits with code 0
- `command_output_contains` — check command output contains an expected string. Preferred over bare `command_succeeds` for tool detection — a command can exit 0 while not returning what we expect
- `version_gte` — check a tool's version meets a minimum requirement. Version string parsing handled by core shared utilities — not reimplemented per plugin
- `python_package_installed` — check a Python package is available in a given environment
- `process_running` — check a named process is currently running

### Network and disk
- `internet_reachable` — basic connectivity check
- `host_reachable` — check a specific host is reachable (for download sources)
- `disk_space_gte` — check available disk space at a path >= N GB. Easy to forget, causes confusing mid-install failures — always check before any large download or build

### Runtime state
- `port_in_use` — check whether a port is bound
- `lock_file_present` — check for a lock file (detect another instance already running)

---

## Instance Model

A **plugin** is a template. An **instance** is a concrete installation.

Each instance has:
- A user-given name (e.g. "llama.cpp — Arc", "llama.cpp — RTX")
- Its own install path
- Its own build-phase settings (compile flags)
- Its own set of profiles (launch-phase settings)
- Per-OS paths (see Cross-OS section below)
- Its own managed venv (for Python-based plugins)

The install path can be either AURINI managed (default — AURINI creates and owns the directory) or custom (user specifies a path, including existing installations). In both cases AURINI manages the instance identically — the distinction is only where the files live on disk. AURINI's own metadata for the instance (profiles, build config, action log) always lives in the AURINI managed path regardless of where the install itself is.

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
    "windows": "C:/llama.cpp/"
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

Plugins reference each other by instance ID, not by path. AURINI resolves paths at runtime. This means a user installing SENNI via AURINI never has to copy-paste paths — AURINI knows where everything is because it installed it.

The `exposes` block in `plugin.json` tells AURINI (and other plugins) what an instance makes available:

```json
"exposes": {
  "binary_path": "bin/llama-server",
  "http_service": {
    "default_port": 8080,
    "health_endpoint": "/health"
  }
}
```

External apps (e.g. SENNI running outside AURINI) can also reference AURINI-managed instances — they just need the path or port, which AURINI can display prominently and offer to copy to clipboard.

### Known instance model limitations (design for these later)

- **External installs** — if the user installs something outside AURINI, AURINI doesn't know about it. An "import existing installation" flow is needed eventually
- **Profile portability** — copying a profile between instances (e.g. Arc → RTX) requires handling flags that are backend-specific. Copy what's compatible, flag what isn't
- **Simple user path** — one instance created automatically, instance management hidden until the user creates a second one. Never let instance complexity leak into the default experience
- **Custom path adoption** — when pointing AURINI at an existing installation, it must detect existing files, back up before touching anything, and offer to adopt (manage in place) vs. reinstall clean. Never silently overwrite an existing directory

---

## Profiles

A profile is a named snapshot of all `launch`-phase settings for a plugin instance. Profiles are the primary way users manage different use cases — different models, different quality/speed tradeoffs, different hardware targets.

### Profile data structure

```json
{
  "profile_id": "gemma-27b-quality",
  "display_name": "Gemma 27B — High Quality",
  "notes": "Best quality, needs full 8GB VRAM free",
  "created": "2026-04-01T14:32:07Z",
  "is_default": true,
  "settings": {
    "model_path": {
      "enabled": true,
      "value": "~/models/gemma-27b-q4.gguf"
    },
    "ctx_size": {
      "enabled": true,
      "value": 8192
    },
    "flash_attn": {
      "enabled": true,
      "value": true
    },
    "gpu_layers": {
      "enabled": true,
      "value": 99
    }
  },
  "custom_args": [
    { "flag": "--threads", "value": "6", "enabled": true }
  ]
}
```

### Profile behaviour

- Every instance has at least one profile. A default profile is created at install time
- One profile is marked as default per instance — used when launching without explicitly choosing
- Switching profiles is instant — no restart required until the next launch
- Changing a profile does not affect other profiles
- Import/export supported — users can share profiles or back them up
- **The model path lives in the profile, not the instance** — this is what makes switching between Gemma and Qwen practical. The entire launch configuration including the model is captured in the profile

### Profile operations

All of these must be smooth and non-destructive:
- Create new profile (blank or duplicated from existing)
- Rename, add/edit notes
- Edit settings within a profile
- Set as default
- Duplicate
- Delete (with confirmation — cannot delete the last profile)
- Export to file
- Import from file

---

## Cross-OS Path Handling

Each instance stores paths per OS. AURINI resolves the correct path for the current OS at runtime.

If a path isn't configured for the current OS, AURINI prompts the user to set it up rather than silently failing. This supports users who run from a shared drive across OS installations — configure both paths once and AURINI just works on either OS.

SENNI already handles this correctly in its own config — the same pattern applies here at the instance level.

---

## Managed Environments

AURINI owns isolated environments for each plugin instance it manages. Users never interact with system-level package managers or Python environments directly.

- **Compiled binary plugins** (e.g. llama.cpp): AURINI owns the build directory and output binaries
- **Python-based plugins** (e.g. Kokoro, Whisper): AURINI provisions and owns a dedicated venv per instance, using the AURINI-managed Python runtime
- The user's existing Python installation and any other environments are never touched
- Uninstall is clean: AURINI removes its managed directory and nothing else

**Implementation note:** The venv management system will be designed and implemented when the first Python-based plugin (likely Kokoro or Whisper) requires it. The llama.cpp plugin is a compiled binary and won't exercise this path. This is intentional — we'll learn from the llama.cpp plugin before designing the venv layer.

---

## Action Log

Every action AURINI takes against the system during a session is recorded in a human-readable action log. The log is:
- Visible to the user in the GUI at any time
- Written to disk so it survives a crash
- Used to drive the undo/revert flow

Each log entry includes: timestamp, action taken, plugin/instance that requested it, outcome, and undo instructions where applicable. Custom argument usage is always noted in the log.

---

## Install Flow State Machine

Every install/update flow follows this sequence. No stage can be skipped.

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

### Summary screen — non-negotiable

Shows before anything touches the system:
- Hardware detected and confirmed
- Each check: passed ✓ / fixed automatically ✓ / needs attention ⚠ / failed ✗
- For anything fixed automatically: what was done and how to undo it
- For anything needing attention: exactly what the user must do, with raw output available to view
- Full list of what is about to happen
- A single "Begin installation" confirmation button

Nothing touches the system until that button is pressed.

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
- Linux: `lspci` output
- Windows: WMIC query
- macOS: `system_profiler`

---

## What Already Exists

### `update_llama.py`
Located at `installation/update_llama.py` in the SENNI repo. This is the prototype of AURINI's llama.cpp plugin — it handles the full install/update flow for Intel Arc on Linux. It represents the pattern that all AURINI plugins should follow.

**What it does:**
1. Asks install vs update, validates path
2. Checks oneAPI is present — if not, shows install instructions (recommends Intel Deep Learning Essentials over full Base Toolkit)
3. Checks render/video group membership — warns if missing with fix command
4. Checks git and cmake
5. Asks parallel build jobs (auto-detects safe default = half CPU cores, allows override)
6. Asks Flash Attention preference (default: off)
7. Asks FP16 preference (default: on — important for 8GB VRAM cards like Arc A750/A770)
8. Detects both untracked files AND modified tracked files before pulling
9. Backs up everything to `~/llama.cpp_backup_YYYY-MM-DD_HH-MM-SS/`
10. Resets modified tracked files (`git checkout .` + `git clean -fd`) so pull proceeds cleanly
11. Shows full summary of what was found and what will happen — **nothing has changed yet at this point**
12. User confirms before anything touches the system
13. Git clone or pull
14. Runs `sycl-ls` to verify GPU is visible before starting a long build
15. Sources oneAPI, runs cmake with exact flags, builds
16. On failure: preserves backups, explains what went wrong, suggests fixes
17. On success: shows binary location, reminds about backup folder

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

### Backup modified tracked files, not just untracked
Git's `status --porcelain` returns both untracked (`??`) and locally modified tracked files (`M`). Early versions of `update_llama.py` only backed up untracked files — this caused a real failure when `examples/sycl/build.sh` (a repo file the developer had modified) blocked `git pull`. Both types must be backed up before any git operation, then modified tracked files reset with `git checkout .`.

### Timestamp on backup folders
`llama.cpp_backup_2026-04-01_14-32-07` not `llama.cpp_backup_2026-04-01`. Running the installer twice in a day would silently overwrite the first backup otherwise.

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

---

## Critical Working Rules

- **Always provide complete files** — never code sections, never snippets, never "find X and replace with Y". The user has ADHD and finds partial edits extremely difficult. Full file replacements only.
- **One file at a time** where possible. Flag upfront if a feature will require touching multiple files and get agreement before proceeding.
- **Stop and check in** if things start going wrong rather than pushing through. Escalating complexity when stuck makes things worse.
- **Never ask the user to remember to do things** at specific times — ADHD means this won't work. Automate it or build it into existing flows instead.
- **Suggest Extended Thinking and/or Opus** when the architecture is genuinely uncertain or a wrong call would cause cascading problems. For most feature work, standard Sonnet is fine.
- **Plugin interface is the blast radius boundary** — flag any change to the plugin contract explicitly before proceeding.
- **Keep Python files single-responsibility and small.** One file per plugin, one file per major core concern. If a file is getting long, that's a signal to split before it becomes a problem.
- **Architecture decisions go in CLAUDE.md with reasoning, not just the decision.** Future sessions won't have this conversation's context — the *why* is as important as the *what*.
- **End every session by updating CLAUDE.md and any relevant design docs.** This is non-negotiable — it's what makes the next session productive.
- **Remind user to push changes** at the end of every session.

---

## TODO / Known Issues

- [ ] Detect if build folder is owned by root and suggest chown fix before attempting build
- [ ] Windows support for llama.cpp SYCL path (`setvars.bat`, `CREATE_NO_WINDOW` — pattern exists in SENNI's `server.py`)
- [ ] NVIDIA/AMD/CPU build paths
- [ ] Uninstall/reinstall flows
- [ ] GUI layer (Tauri)
- [ ] `aurini_core` base classes (`AuriniPlugin`, `CheckResult`, `RemedyResult`) — **start here**
- [ ] Core check library implementation
- [ ] Action log implementation
- [ ] Profile management implementation
- [ ] Instance management implementation
- [ ] llama.cpp plugin (port from `update_llama.py`)
- [ ] SENNI plugin
- [ ] Kokoro TTS plugin (planned — important for SENNI)
- [ ] Managed venv system (implement when first Python-based plugin requires it)
- [ ] Per-instance Python versions (future — implement only if version conflict cases emerge)
- [ ] "Import existing installation" flow for externally installed components
- [ ] Profile portability between instances (copy compatible settings, flag incompatible ones)
- [ ] Model file management (download, organise, delete)
- [ ] Python runtime update UI (Settings → Components)

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

The `exec` replaces the shell with llama-server so the PID is the actual target process (important for clean shutdown).

---

## Useful Links

- [llama.cpp SYCL backend docs](https://github.com/ggml-org/llama.cpp/blob/master/docs/backend/SYCL.md)
- [Intel oneAPI Base Toolkit](https://www.intel.com/content/www/us/en/developer/tools/oneapi/base-toolkit.html)
- [Tauri docs](https://tauri.app/start/)
- [SENNI repo](https://github.com/sdesser/senni) — see `scripts/config.py` for hardware detection, `scripts/server.py` for launch logic, `installation/update_llama.py` for the llama.cpp installer prototype
