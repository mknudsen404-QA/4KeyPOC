# Auto-install plan — "plug in, click once, it runs"

Written 2026-09-14. Supersedes the architecture sections of
`plug-and-play-installer-plan.md` (whose spike findings remain valid and
are relied on below). Handoff document for an implementing agent.

## Goal

On a machine that has never seen Switchboard:

1. Plug the board in. A small `SWITCHBD` drive appears (macOS Finder /
   Windows Explorer).
2. The user double-clicks the installer for their OS and gets **one native
   consent dialog** that says what will be installed, where, and that
   nothing needs admin rights.
3. On "Install": the bridge is downloaded, installed per-user, registered
   to start at login, started immediately, and self-verified. A final
   dialog reports success (or failure with a log path).
4. Forever after: plug in → keys light within seconds. No terminal.

Zero-click autorun is impossible on every modern OS (see the earlier plan
doc — this is an OS security floor). One double-click plus one consent
dialog is the ceiling, and it is exactly what printers and dongles have
shipped for a decade.

## What exists today and what changes

| Today | After this plan |
|---|---|
| `firmware/neokey` = CDC only; `firmware/msc_cdc_spike` = CDC+MSC with no keys | One firmware: NeoKey + composite CDC+MSC |
| Volume holds one file, `Install Switchboard.command`, which `git clone`s and runs `host/setup.sh` | Volume holds a macOS stub, a Windows stub, and a README; stubs fetch a versioned release, never `git` |
| `host/setup.sh` is the install logic (bash, macOS-only) | `switchboard install` (Python, cross-platform) is the single install logic; `setup.sh` becomes a 5-line wrapper |
| Install requires `python3` → on a fresh Mac that triggers the Xcode CLT prompt (the laptop install worked only because it already had CLT + Python 3.13, i.e. a dev machine) | Bootstrap uses `uv` to provide a pinned standalone Python — no CLT, no system Python, same path on both OSes |
| LaunchAgent only | `ServiceManager` protocol: launchd on macOS, Scheduled Task on Windows |
| Bridge is macOS-only (termios, fcntl, AppleScript, Quartz, `ps`) | Core is portable; macOS/Windows adapters behind existing Protocols |
| No uninstall, no upgrade, no install manifest | Transactional install with manifest, `switchboard upgrade`, `switchboard uninstall` |

## Architecture: four layers, each with one job

```
[0] On-device stub      firmware/installer_volume/*     tiny, dumb, per-OS, baked into flash — changes rarely
        │  fetches ONE stable URL over HTTPS
[1] Bootstrap           installer/bootstrap.{sh,ps1}    ensure uv → fetch pinned release → verify sha256 → run [2]
        │
[2] switchboard install host/switchboard/install/*      cross-platform Python, transactional, tested
        │  uses
[3] Platform adapters   host/switchboard/platform/*     ServiceManager, Dialog, Paths (macOS / Windows impls)
```

Why the split (SOLID applied, not just cited):

- **Single responsibility**: the stub only knows how to show consent and
  fetch one URL. The bootstrap only knows how to obtain a runtime and a
  verified release. `install` only knows install steps. Adapters only
  know their OS.
- **Open/closed**: adding Linux later means one more adapter set and one
  more stub file; nothing above it changes.
- **Liskov**: every adapter implements a `Protocol` and has a `Fake*`
  twin for tests, matching the existing `FakeDevice` / `FakeTerminal` /
  `FakeProber` pattern already in the repo.
- **Interface segregation**: `ServiceManager`, `Dialog`, `Paths`,
  `Locker` are separate small protocols; the installer depends on
  exactly the ones it needs.
- **Dependency inversion**: `install` receives adapters via a
  `Platform` bundle chosen in one place (`platform/__init__.py:current()`),
  never imports `launchctl`/`schtasks` directly.
- **DRY**: `setup.sh`, the stub, and the bootstrap contain no install
  logic. Status/hook tables stay generated from `status_table.py`. The
  FAT image is generated from a directory, not hand-assembled per file.

The "ACID" you asked for maps onto the installer like this:

- **Atomic**: download and build into `<app>/staging/<version>/`, verify,
  then a single rename to `<app>/versions/<version>/` and a `current`
  symlink/junction swap. Failure before the swap leaves the previous
  install untouched.
- **Consistent**: post-install `switchboard doctor --strict` must pass;
  if it doesn't, the swap is reverted and the failure dialog shows.
- **Isolated**: an exclusive lock file in the app dir makes a second
  double-click (or a re-plug mid-install) wait/no-op instead of racing.
- **Durable**: `manifest.json` records version, checksum, install time,
  service label, and every file/registration written — which is what
  `uninstall` and `upgrade` read. Structured `install.log` next to it.

## Trust model (state it, don't hand-wave it)

- Stub → bootstrap: HTTPS to a fixed GitHub URL
  (`https://github.com/mknudsen404-QA/4KeyPOC/releases/latest/download/bootstrap.sh`
  and `.ps1`). The stub cannot pin a hash (it's in flash; the repo will
  change), so this hop trusts TLS + GitHub. Say so in the consent dialog.
- Bootstrap → release: pinned tag, `SHA256SUMS` from the same release,
  verified before anything is unpacked. Bootstrap refuses to proceed on
  mismatch.
- `uv` install uses its official installer script pinned to a version
  and its published checksum; this is the one third-party dependency
  added. Decision to confirm: acceptable for v1 (recommended — it
  removes the Xcode CLT and "which Python" problems on both OSes in one
  move). If not acceptable, fallback is PyInstaller one-file binaries per
  OS (Phase 6, listed as optional).
- Everything is per-user. No `sudo`, no UAC, no writes outside
  `~/Library/Application Support/Switchboard` (macOS) or
  `%LOCALAPPDATA%\Switchboard` (Windows).
- Uninstall removes everything the manifest lists and nothing else.
- Code signing / notarization / SmartScreen reputation: out of scope for
  v1. Because the payload arrives via `curl`/`Invoke-WebRequest` (no
  quarantine xattr, no Mark-of-the-Web on FAT volumes), Gatekeeper and
  SmartScreen do not block the current flow — verified for macOS in the
  spike; **verify on Windows in Phase 5.**

## Ground rules for the implementer

- Never flash hardware without the human saying "flash it now" in that
  turn. All firmware phases are compile-only until then.
- Do not hand-edit generated files (`status_table.h`, `installer_disk.h`).
- Keep the JSON-lines protocol unchanged. Firmware `boot` events (added in
  Recovery 3) are relied on by `doctor` and by post-install verification.
- Every host change: tests in `host/tests/`, `pytest -m "not hardware"`
  green on **macOS and Windows** CI once Phase 4 lands.
- Commit per sub-phase, prefixed `Install N.x:`.
- Don't break today's working path (`git clone` + `host/setup.sh`) until
  the new path is verified end-to-end on a fresh macOS account.

## Phase 1 — Cross-platform core (prerequisite for Windows; no behaviour change on macOS)

Goal: `python -m pytest host/tests` passes on Windows with the macOS-only
pieces swapped for adapters. Nothing user-visible changes on macOS.

Files: `host/switchboard/device.py`, `registry.py`, `liveness.py`,
`terminal.py`, new `host/switchboard/platform/`, `host/pyproject.toml`
(new — the package needs to be installable; `setup.sh` currently runs it
from the checkout).

1.1 **Serial**: replace the `termios`/`fcntl`/`select` implementation in
`SerialDevice` with `pyserial` (`serial.Serial`, `serial.tools.list_ports`).
Port discovery becomes VID/PID based (`0x303A`, any PID) with the old glob
patterns as fallback, so `/dev/cu.usbmodemNNNN` vs `COM7` is no longer
special-cased anywhere. `DeviceLink` protocol and `FakeDevice` unchanged.

1.2 **Registry lock**: extract a `Locker` protocol; `FcntlLocker`
(POSIX) and `MsvcrtLocker` (Windows) — or use `portalocker` if a
dependency is acceptable (recommended: `portalocker`, it is small and
handles both). `Registry` takes a locker; default chosen by platform.

1.3 **Process liveness**: `ProcessProber` currently shells to `ps`. Use
`psutil` (`pid_exists`, `Process.status`, `cmdline`) — one implementation
for both OSes, fewer subprocess timeouts. Keep `FakeProber`.

1.4 **Terminal driver**: `AppleScriptTerminal` stays. Add
`WindowsTerminalDriver` (launch via `wt.exe -w 0 new-tab --title <slot>
<cmd>`, fall back to `start` + `conhost` if `wt` absent). Focus and tab
close are best-effort in v1 (Windows Terminal has no scripting API for
"focus tab N"; document the limitation, keep `NullTerminal` as the
degrade path).

1.5 **Push-to-talk key injection**: extract a `KeyInjector` protocol with
`hold(key)` / `release(key)` semantics (not `post_key(down)`), so the
"hold" is owned by the injector and it can emit whatever the OS needs to
look like a physically held key (see the PTT section below — this is
also the fix for the current PTT bug). macOS impl via Quartz, Windows
impl via `ctypes.windll.user32.SendInput`, `FakeKeyInjector` for tests.

1.6 **Paths**: new `Paths` protocol with `app_dir()`, `log_dir()`,
`config_dir()`, `claude_settings()`, `codex_home()`. Replaces the scattered
`Path.home() / "Library/..."` and `~/.claude` literals. `hooks_install.py`
already uses `~/.claude` which is the same on Windows.

1.7 **Hook server** binds `127.0.0.1:8877` — unchanged; confirm Windows
Firewall doesn't prompt for loopback (it shouldn't).

1.8 **Packaging**: `host/pyproject.toml` declaring `switchboard` as a
package with a `switchboard` console script, dependencies split by marker:
`pyobjc-framework-Quartz; sys_platform == "darwin"`, `pynput;
sys_platform == "win32"`, `pyserial`, `psutil`, `portalocker`. This is what
the bootstrap installs with `uv`.

Acceptance: all existing tests green on macOS; new adapter tests with
fakes; CI matrix `macos-latest` + `windows-latest` running host tests
(firmware compile stays macOS-only). No change to how the LaunchAgent
runs the bridge yet.

## Phase 2 — `switchboard install` / `upgrade` / `uninstall` (transactional installer in Python)

Files: `host/switchboard/install/{__init__,plan,steps,manifest,transaction}.py`,
`host/switchboard/platform/{service,dialog}.py`, `host/switchboard/cli.py`,
tests under `host/tests/test_install*.py`, `host/setup.sh` (becomes a wrapper).

2.1 **`ServiceManager` protocol**: `install(spec)`, `start()`, `stop()`,
`status()`, `uninstall()`. `LaunchdServiceManager` wraps what
`install_bridge_launch_agent.py` does today (move that logic in; leave the
script as a thin shim for one release, then delete). `SchtasksServiceManager`
registers a per-user logon task (`schtasks /Create /SC ONLOGON /RL LIMITED
/TN Switchboard\Bridge /TR "<pythonw> -m switchboard listen ..."`), plus
`start` = `schtasks /Run`. Both have `FakeServiceManager`.

2.2 **`Dialog` protocol**: `consent(title, body) -> bool`, `notify(title,
body, level)`. `OsascriptDialog` (macOS), `WindowsDialog`
(`ctypes.windll.user32.MessageBoxW`), `TtyDialog` (fallback when no GUI,
e.g. SSH/CI), `FakeDialog`.

2.3 **Install steps as objects** (`Step` with `describe()`, `check()`,
`apply(tx)`, `rollback(tx)`), executed by a `Transaction` that appends to
the manifest journal as each step commits. Steps, in order:

1. `AcquireLock`
2. `EnsureLayout` (app dir, log dir)
3. `StageRelease` (already unpacked by bootstrap → verify checksum again
   against manifest input)
4. `CreateVenv` (`uv venv` + `uv pip install <staged wheel/sdist>`)
5. `WriteConfig` (`agents.json` from example if absent — never clobber)
6. `InstallHooks` (existing `hooks_install`, now idempotent-verified)
7. `RegisterService` (via `ServiceManager`)
8. `SwitchCurrent` (atomic rename/junction swap — the commit point)
9. `StartService`
10. `Verify` (`doctor --strict --timeout 15`; waits for a `boot` or
    protocol line from the board if one is plugged in, otherwise checks
    service + port-watcher health only)

`upgrade` = same steps with a new version dir, previous `current` retained
for rollback; `uninstall` = reverse of the manifest.

2.4 **`doctor --strict`** exits non-zero on any FAIL and is the
installer's consistency check. Add a `service` check (via
`ServiceManager.status()`).

2.5 `host/setup.sh` → wrapper: `exec installer/bootstrap.sh --from-checkout "$PWD/.."`
so dev installs and end-user installs run the identical Python path
(DRY), just sourcing the release from the working tree instead of GitHub.

Acceptance: `switchboard install --dry-run` prints the plan; full install
into a temp `HOME` with fakes for service/dialog passes tests on both
OSes; simulated failure at each step leaves no partial state (test per
step); `uninstall` after install leaves the temp `HOME` byte-identical to
before (golden test).

## Phase 3 — Bootstrap scripts and release pipeline

Files: `installer/bootstrap.sh`, `installer/bootstrap.ps1`,
`.github/workflows/release.yml`, `installer/README.md`.

3.1 `bootstrap.sh` (bash 3.2-compatible — macOS ships bash 3.2) and
`bootstrap.ps1` (PowerShell 5.1-compatible — that's what Windows 10/11
ship). Identical structure, each ~100 lines:

1. Parse `--version <tag>|latest`, `--from-checkout <dir>`, `--dry-run`,
   `--yes` (skip consent for CI).
2. Consent dialog (osascript / MessageBox) unless `--yes`. The text names
   the install dir, the URL being fetched, "no administrator rights",
   and how to uninstall.
3. Ensure `uv` in `<app_dir>/tools/uv` (do **not** touch the user's PATH
   or any global install; pinned version + checksum).
4. Resolve tag → download `switchboard-<tag>.tar.gz` + `SHA256SUMS`,
   verify with `shasum -a 256` / `Get-FileHash`.
5. Unpack into `<app_dir>/staging/<tag>/`.
6. `uv run --python 3.12 --with <staged sdist> switchboard install
   --release <tag> --staged <dir> [--yes]`.
7. Exit with install's exit code; the Python side owns the final dialog.

3.2 Release workflow: on tag `v*`, build sdist + wheel from `host/`,
generate `SHA256SUMS`, attach both bootstrap scripts, publish a GitHub
Release. `releases/latest/download/bootstrap.sh` is the stable URL the
stub uses.

3.3 Versioning: single source in `host/pyproject.toml`; firmware reports
its own version in the `boot` event; the bridge logs a warning on a
major-version mismatch (protocol compatibility is versioned separately as
`protocol_version` in the `boot` event — add it now, it's cheap).

Acceptance: `bootstrap.sh --dry-run --yes` and `bootstrap.ps1 -DryRun -Yes`
run in CI on both OSes and produce the same plan text; a real tagged
pre-release (`v0.1.0-rc1`) installs on the dev Mac from the public URL.

## Phase 4 — Firmware: merge composite CDC+MSC into `firmware/neokey`

Files: `firmware/neokey/neokey.ino`, new `firmware/neokey/usb_msc.{h,cpp}`,
`firmware/installer_volume/` (source dir for the FAT image),
`firmware/tools/gen_fat.py` (moved/generalised from the spike),
`firmware/neokey/installer_disk.h` (generated), `firmware/neokey/README.md`,
`.github/workflows/ci.yml`.

4.1 Generalise `gen_fat.py` to build a FAT12 image from a **directory**
(multiple LFN files, ≤ ~64 KB total), with a pure-Python unit test that
parses the image back (boot sector, FAT chain, directory entries) —
no `hdiutil` needed in CI. Keep the volume label `SWITCHBD`.

4.2 Volume contents (`firmware/installer_volume/`):

- `Install Switchboard.command` (macOS): consent-free; just
  `curl -fsSL <stable-url>/bootstrap.sh | bash` **after** an osascript
  consent dialog (the stub owns the *first* consent so nothing downloads
  without a click; bootstrap runs with `--yes` when invoked by the stub).
- `Install Switchboard.cmd` (Windows): `powershell -NoProfile
  -ExecutionPolicy Bypass -Command "irm <stable-url>/bootstrap.ps1 | iex"`
  after a `MessageBox` consent (via a one-line PowerShell in the .cmd).
- `README.txt`: what this is, the two-line manual alternative, uninstall
  instructions, project URL.
- No `autorun.inf` — ignored by modern Windows and flagged by AV.

The stubs must be generic: they embed the stable URL only, not a version.

4.3 Firmware: switch `firmware/neokey` to `USBMode=default` (TinyUSB) so
MSC is possible, add the MSC callbacks from the spike behind
`usb_msc.cpp`, call `USB.begin()` after `MSC.begin()`. `Serial` becomes
TinyUSB CDC; the seesaw/I²C path is unchanged. Update
`test/compile.sh` FQBN and the README's board settings.

Risks to check on the real board (compile-only until sign-off): TinyUSB
CDC + I²C timing (the spike showed 19% RAM, no I²C); `if (Serial)`
gating of the `boot` event (TinyUSB CDC reports "connected" only after
DTR — emit boot lines regardless, don't gate on `Serial`); the bootloader
auto-reset unreliability already documented for TinyUSB mode.

4.4 Keep "advertise MSC forever" for v1 (decision already taken). Note
for later: TinyUSB can stop presenting MSC by not calling `MSC.begin()`
when a flag in NVS says "installed"; the bridge could set that flag via a
`config.set` line. Out of scope now.

4.5 CI: compile `firmware/neokey` with the new FQBN; run the FAT image
test; assert `installer_disk.h` is up to date with `installer_volume/`
(regenerate and `git diff --exit-code`).

Acceptance: compiles; image test passes; on sign-off, flashed to the dev
board and (a) `SWITCHBD` mounts with three files, (b) keys and LEDs work
exactly as before, (c) `boot` events and protocol traffic flow over the
TinyUSB CDC port, (d) `doctor` passes.

## Push-to-talk: the current bug, the fix, and first-run onboarding

Observed on the laptop (2026-09-14): first PTT press triggered macOS's
microphone permission prompt for Terminal (the app hosting Claude Code),
but every hold after that did nothing, while holding the physical space
bar in the same session records fine. `/voice` also had to be enabled by
hand in the session first.

### Root cause (high confidence; confirm with the 10-minute experiment)

`AppleScriptTerminal.post_key` posts exactly one `keyDown` on hold-start
and one `keyUp` on hold-stop (`host/switchboard/terminal.py`). A terminal
application never sees key-up events — it only receives bytes — so Claude
Code's hold-to-talk must infer "still held" from the OS **key autorepeat**
stream (a physically held key delivers a space byte every ~30 ms) and
"released" from the repeats stopping. Synthetic `CGEvent`s do not
autorepeat; autorepeat is generated by the HID layer for physical keys
only. Net effect: each PTT hold delivers one space byte, i.e. a tap. The
very first tap was enough for Claude to begin opening the microphone
(hence the permission prompt), and every subsequent tap fell below the
hold threshold.

Experiment (run before writing the fix): with a Claude session in
Terminal, `/voice` on, and the bridge's venv Python trusted for
Accessibility, run a snippet that (a) posts a single keyDown, waits 2 s,
posts keyUp; then (b) posts keyDown with `kCGKeyboardEventAutorepeat` set
every 33 ms for 2 s, then keyUp. If (b) records and (a) does not, the
diagnosis is confirmed. Also try (b) without the autorepeat flag — some
apps ignore the flag and only care about cadence.

### Fix (Phase 1.5 `KeyInjector`)

- `hold(key)` starts a background repeater posting keyDown at the OS
  repeat cadence (macOS: read `KeyRepeat`/`InitialKeyRepeat` defaults;
  default 33 ms after a 250 ms initial delay — replicate exactly so it
  looks physical); `release(key)` stops the repeater and posts keyUp.
  Windows impl: `SendInput` with the same cadence.
- Bound the hold: auto-release after a configurable max (default 120 s)
  so a lost `voice.hold.stop` (unplug mid-hold) can never leave a key
  stuck down. Bridge logs when this fires.
- Wait for focus to settle: `Focus(tty)` returns before Terminal has
  actually switched tabs. Poll `frontmost` + selected-tab tty for up to
  300 ms before starting the repeater, else log and abort the hold. This
  is the second most likely cause of "sometimes goes to the wrong tab".
- Tests: `FakeKeyInjector` records cadence; reducer test that hold-stop
  without hold-start is a no-op; bridge test that the watchdog releases.

### `/voice` enablement — users will not know to type it

Today the user must type `/voice` in each session before PTT works, and
the README explains why an earlier auto-toggle was removed (TUI redraws
made detection unreliable and once landed `/voice` as a billed chat
turn). Plan, in order of preference — the implementer stops at the first
that works:

A. **Persistent setting spike (30 min).** Check current Claude Code docs
   and `claude config` / `~/.claude/settings.json` for a way to default
   voice mode on (setting, env var, or CLI flag). If one exists, the
   installer's `InstallHooks` step becomes `ConfigureClaude` and merges it
   with the same idempotent merge `hooks_install.py` uses. This removes
   the problem entirely. Also check Codex.
B. **Deterministic one-shot at launch.** The bridge already receives the
   `SessionStart` hook for every slot it launches. On `SessionStart` for a
   freshly launched, bridge-owned tab, wait for the prompt to render
   (bounded delay, then verify the tab is idle via the existing
   `UserPromptSubmit`/`Stop` state — no byte-tailing), send `/voice⏎`
   once, and record `voice: "requested"` in the registry for that slot.
   This is different from the removed approach: it never toggles based on
   detected state, runs exactly once per launch into a known-idle prompt,
   and is off by default until Phase 5 validates it (`--voice-autoenable`).
C. **Guided fallback (always implemented, regardless of A/B).** If PTT is
   pressed on a slot whose voice state is unknown/off, key D blinks
   amber for 2 s and the bridge posts a macOS notification: "Type /voice
   in Agent N's terminal to enable push-to-talk, then hold again."
   Add a `voice.hint` status to `status_table.py` so the LED behaviour is
   generated, not hand-coded.

### Permissions onboarding (folds into Phase 2's `Verify` step)

PTT needs three grants a first-time user will not discover on their own,
and two of them are prompted to a process the user won't recognise
("python3 wants to…"):

| Grant | Who macOS attributes it to | When it must be triggered |
|---|---|---|
| Automation → Terminal (osascript) | the bridge's Python | install time, deliberately |
| Accessibility (CGEventPostToPid) | the bridge's Python | install time, deliberately |
| Microphone | Terminal (hosts Claude) | first real hold — unavoidable, but the success dialog should say it's coming |

Add an explicit `RequestPermissions` step between `RegisterService` and
`StartService`: from the **same interpreter path the service will run**,
call `AXIsProcessTrustedWithOptions(prompt=True)` and run a trivial
`osascript` against Terminal, each preceded by a `Dialog.notify`
explaining the prompt the user is about to see and why. Re-use
`doctor.check_accessibility` / `check_terminal_automation` as the
post-conditions. If either is still denied after the prompts, the install
still succeeds (LEDs and launch work without them) but the success dialog
says PTT is disabled and how to enable it — and `doctor` shows it as WARN,
not FAIL. Note: TCC keys these grants to the executable, so `upgrade` must
keep the interpreter path stable (`<app>/current/.venv/bin/python3` via
the `current` link) or the grants are lost on every upgrade.

### Hardware note

The Mac mini has no built-in microphone, which is why voice can only be
tested on the laptop today. Any class-compliant USB microphone (~$15-25 at
an office-supply or big-box store) makes the mini a full test bench: no
driver, shows up in System Settings → Sound → Input immediately. Worth
buying before starting the PTT fix so the experiment above and Phase 5.1
can run on the dev machine.

### Windows notes (design only — no hardware to validate)

- Windows Terminal/conhost also deliver bytes only, so the same
  autorepeat approach applies; `SendInput` with `KEYEVENTF_*` at the
  cadence from `SystemParametersInfo(SPI_GETKEYBOARDSPEED)`.
- No Accessibility/Automation equivalent: `SendInput` works without a
  grant unless the target is elevated (it won't be). Microphone consent
  is per-app in Settings → Privacy → Microphone and is prompted to the
  terminal app, same shape as macOS.
- Focus: `SetForegroundWindow` is restricted to the foreground process's
  lineage; the bridge may need `AllowSetForegroundWindow` handshakes or
  to accept "focus best-effort" for v1, as already scoped in 1.4.

## Phase 5 — End-to-end validation

5.1 macOS fresh-account test on the laptop: create a new standard user,
log in, plug in, double-click, Install → success dialog → keys light after
launching an agent. Then re-plug (should no-op), `switchboard uninstall`,
confirm nothing left. PTT: with a USB mic on the mini or on the laptop,
verify the permission prompts appear in the order the installer
announces them, that a 3 s hold records and transcribes, and that
unplugging mid-hold releases the key (watchdog). Record the exact prompts macOS shows (Accessibility
for PTT, Automation for Terminal) and fold the guidance into the success
dialog.

5.2 Windows test (needs a Windows 10/11 machine or VM): drive appears as a
letter; `.cmd` runs; `usbser` binds the CDC interface (Device Manager
shows a COM port — if it shows an unknown device, the composite descriptor
needs a fix in firmware); install succeeds; Scheduled Task starts at
logon; keys light on agent launch. PTT and terminal focus: document what
works.

5.3 Failure-path tests (both OSes): no network; wrong checksum (tamper the
download); double double-click; unplug during install; install when the
old LaunchAgent from `setup.sh` is present (must migrate or refuse
clearly, never run two bridges).

5.4 Docs: `host/README_BRIDGE.md` "Setting up on a new Mac" → "Installing"
with the three paths (board, one-liner, from checkout); `installer/README.md`
for maintainers (how to cut a release, how stubs find the release).

## Phase 6 (optional) — Standalone binaries

If `uv` is rejected or the Python runtime download proves flaky: PyInstaller
one-file per OS built in `release.yml`, bootstrap downloads the binary
instead of a sdist, installer steps unchanged (`CreateVenv` becomes a
no-op step — the Step abstraction is what makes this a swap, not a
rewrite). Adds code-signing questions; defer.

## Suggested order and parallelism

- **Do the PTT fix first** (the experiment, then `KeyInjector` from 1.5):
  it is a standalone bug on the working macOS path, needs no installer
  work, and unblocks voice for the current laptop install immediately.
  Windows has no test machine today, so Windows work stops at CI-green
  in Phase 1 plus the design notes; nothing Windows-specific is claimed
  to work until Phase 5.2 runs on real hardware.
- Phase 1 and Phase 4.1/4.2 (FAT generator + volume contents) are
  independent — can run in parallel.
- Phase 2 depends on 1.6/1.8. Phase 3 depends on 2.
- Phase 4.3 (firmware merge) can be compile-ready any time but should be
  flashed only after Phase 3 has a real release for the stub to fetch —
  otherwise the volume's installer points at nothing.
- Phase 5 last.

## Decisions (made 2026-09-14 by the owner)

1. Python runtime: `uv`-managed pinned standalone Python. PyInstaller
   binaries stay optional (Phase 6).
2. Install location: `~/Library/Application Support/Switchboard` (macOS),
   `%LOCALAPPDATA%\Switchboard` (Windows). The `~/4KeyPOC` checkout path
   remains a developer path only, via `setup.sh --from-checkout`.
3. Windows v1 scope: install + LEDs + agent launch; PTT and tab focus are
   best-effort and documented as such. No Windows hardware exists today,
   so Windows work stops at CI-green plus design notes until Phase 5.2.
4. Consent: one native dialog total. Stubs invoke bootstrap with `--yes`.
5. `/voice` enablement: run the persistent-setting spike (option A)
   first; if nothing exists, implement the deterministic one-shot
   `/voice⏎` on `SessionStart` (option B) **behind a flag**
   (`--voice-autoenable`, default off until Phase 5.1 validates it), and
   always ship the guided hint (option C) as the fallback.
6. A USB microphone will be added to the Mac mini so PTT work does not
   depend on the laptop.

## Out of scope

- Zero-click autorun (impossible), code signing/notarization, Linux,
  hiding the MSC volume after install, auto-update on a timer (manual
  `switchboard upgrade` only in v1).
