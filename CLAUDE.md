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
- **Safe by default** — back up before modifying, never delete without explicit instruction, always preserve custom files
- **Guided, not just documented** — don't just error out, explain what went wrong and what to do next
- **Modular** — new components (Kokoro, Whisper, etc.) are self-contained plugins; the core tool provides the framework

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

---

## Plugin Architecture

Each component (llama.cpp, Kokoro, Whisper, etc.) is a self-contained plugin module. Plugins declare:
- Their dependencies
- Supported hardware backends and their requirements
- Install steps per platform/hardware combination
- Configuration options and what they mean in plain English
- How to verify a successful install
- How to launch
- How to uninstall/reinstall cleanly

The AURINI core provides:
- Hardware detection (GPU vendor, VRAM, OS)
- The install/update/uninstall framework
- Backup and safety systems
- The GUI layer that renders plugin-declared options
- Shared utilities (git operations, cmake builds, file management)

**SENNI plugin:** Will tell AURINI how to configure llama.cpp specifically for SENNI's needs, and how to wire in Kokoro TTS when that's ready. A user installing SENNI via AURINI would get everything configured correctly automatically.

---

## Hardware Detection Strategy

Detect at startup, present to user for confirmation before doing anything:

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
Every flow must show a "here's what we found, here's what will happen" screen before touching the system. This is non-negotiable. Non-technical users are stressed by installers that do things silently. Seeing a clear summary with a "shall we begin?" confirmation builds trust.

### Backup modified tracked files, not just untracked
Git's `status --porcelain` returns both untracked (`??`) and locally modified tracked files (`M`). Early versions only backed up untracked files — this caused a real failure when `examples/sycl/build.sh` (a repo file the developer had modified) blocked `git pull`. Both types must be backed up before any git operation, then modified tracked files reset with `git checkout .`.

### Timestamp on backup folders
`llama.cpp_backup_2026-04-01_14-32-07` not `llama.cpp_backup_2026-04-01`. Running the script twice in a day would silently overwrite the first backup otherwise.

### FP16 default ON
Most users running local models have consumer cards with limited VRAM (8-12GB). FP16 halves compute buffer VRAM usage with negligible quality impact. Defaulting it off would be wrong for the majority of users.

### Explicit compiler flags in cmake
`-DCMAKE_C_COMPILER=icx -DCMAKE_CXX_COMPILER=icpx` must be passed explicitly even after sourcing setvars.sh. Without them cmake may fall back to system gcc/g++ and produce a broken SYCL build silently.

### sycl-ls verification before building
A 20-30 minute build that was always going to fail due to GPU not being visible is a terrible user experience. Run `sycl-ls` first, check for `level_zero:gpu`, warn if not found, give the user a chance to cancel.

### Intel Deep Learning Essentials over full Base Toolkit
The official llama.cpp SYCL docs recommend this lighter package for llama.cpp specifically. It has everything needed and is significantly smaller than the full oneAPI Base Toolkit. Always present it as the recommended option.

### render/video group membership check
Intel GPU access on Linux requires the user to be in these groups. Check early, warn clearly with the exact fix command (`sudo usermod -aG render,video $USER`), explain that a logout/login is required. Don't block the build (it affects runtime not build time) but make sure the user knows.

### Never require sudo to build
If the build folder has wrong ownership (e.g. was created by a previous sudo run), cmake will fail with Permission Denied. The fix is `sudo chown -R $USER:$USER ~/llama.cpp/build` — but this should be detected and suggested by the script rather than leaving the user confused. (TODO: add this check to the script)

---

## TODO / Known Issues

- [ ] Detect if build folder is owned by root and suggest chown fix before attempting build
- [ ] Windows support (Intel SYCL path uses `setvars.bat` and `CREATE_NO_WINDOW` — pattern already exists in SENNI's `server.py`)
- [ ] NVIDIA/AMD/CPU build paths
- [ ] Uninstall/reinstall flows
- [ ] GUI layer (Tauri)
- [ ] Formal plugin interface definition
- [ ] SENNI plugin
- [ ] Kokoro TTS plugin (planned — important for SENNI)
- [ ] Model file management (download, organise, delete)
- [ ] Launch argument GUI (translate flags into plain English options)

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
