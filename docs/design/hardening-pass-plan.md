# Hardening pass — second unit prep

Written 2026-10-03. Handoff document for an implementing agent. Scope set
by the owner in planning conversation: macOS-only install polish (not the
full cross-platform `auto-install-plan.md`), Codex voice fixed now with
Antigravity scoped as a research spike, log rotation closed for real, the
settings UI made discoverable without a terminal, and the 4key board
pushed to fab-ready.

This supersedes nothing — `auto-install-plan.md`, `family-capability-matrix.md`,
and `hardware/kicad/README.md` remain the source of truth for the larger
plans this pulls from. This document is the near-term slice actually being
built now, ahead of shipping a second unit.

## Workstream A — Voice: Codex now, Antigravity scoped

**Status (2026-10-03): A1 done, A2 done and went further than scoped —
confirmed live, not just spiked.** See `docs/design/family-capability-matrix.md`
for the full writeup. Summary:

- A1: Codex voice confirmed working end-to-end (macOS Dictation bound to
  F13, held via the board's PTT key, transcribed into a real Codex
  session). Two real bugs fixed along the way: `doctor.py` imported
  `AXIsProcessTrusted` from the wrong module (always reported
  Accessibility as ungranted even when it wasn't), and the Dictation
  availability check used a macOS preference key that no longer exists on
  macOS 26.
- A2: went from "research spike" to "shipped" in the same session —
  Antigravity CLI (`agy`, via `brew install --cask antigravity-cli`) now
  has a real `AntigravityProfile`, registered in the family registry,
  with effort control (clamped to what its default Gemini model actually
  supports) and voice PTT confirmed live the same way as Codex. Hooks are
  explicitly NOT wired (a real, scoped gap — Antigravity's hook mechanism
  is structurally different: project-local config file, synchronous
  execution that blocks the agent loop, a required JSON response contract
  per event, no session-start/session-end signal at all) — tracked in
  `families/antigravity.py`'s docstring for whoever picks it up next.
- A structural bug in the voice-PTT plumbing itself was found and fixed
  while verifying both of the above: the single shared `key_injector`
  posts events to one specific process (`CGEventPostToPid`) — correct for
  `claude_native` typing into Claude's own terminal, but silently
  incapable of triggering a system-wide OS shortcut like a configured
  Dictation hotkey (`CGEventPostToPid` bypasses global-hotkey dispatch
  entirely). `VoiceContext` now carries a second injector,
  `global_key_injector`, posting via `CGEventPost(kCGHIDEventTap, ...)`.
  This is what actually made the Codex and Antigravity verifications
  above possible, not just the chord/availability fixes.

**A1. Make Codex voice actually work (same-session fix, not a design problem).**
Per `family-capability-matrix.md`'s Phase 4 update, the `hotkey` provider is
wired and correct but has nothing to drive — no dictation app is installed
on this Mac, and built-in macOS Dictation has never been enabled
(`defaults read -g AppleDictationAutoEnable` returns "does not exist").

1. Enable built-in macOS Dictation (System Settings → Keyboard → Dictation)
   as the zero-install path — no new app, no license. Confirm its default
   shortcut doesn't collide with anything Switchboard already uses, and
   point `hotkey`'s `chord` at whatever that shortcut is (`switchboard/voice/hotkey.py`'s
   docstring names the exact check to redo).
2. Re-run Phase 4.4's validation: a Codex slot with `provider: hotkey`
   actually recording and landing transcribed text in the Codex prompt on
   a hold.
3. Update `doctor` / `/api/voice-providers` output and
   `family-capability-matrix.md` with the confirmed-live result — these
   already report availability honestly, they just need the underlying
   fact to flip from false to true.
4. If built-in Dictation proves unreliable (accuracy, latency, or requires
   per-hold network round-trip to Apple's servers in a way that feels
   bad), fall back to evaluating one real third-party app (Superwhisper is
   the most relevant price/quality point) as a second supported option —
   `hotkey` already doesn't care which app owns the shortcut.

**A2. Antigravity family-profile spike (research, not yet implementation).**
No `FamilyProfile` exists for Antigravity today — `grep` confirms zero
references anywhere in `host/switchboard/`. Follow the exact method
`family-capability-matrix.md` used for Gemini/Kimi: read Antigravity's own
docs/`--help`, install it for real if a CLI/terminal surface exists, and
fill in the same matrix row before writing code:

- Does it expose a CLI/terminal mode at all, or is it IDE-only (it's a
  VS Code-family agentic IDE — confirm whether any part of its agent loop
  is scriptable from a terminal session Switchboard could launch/observe).
- Lifecycle hooks: do any exist, what events, what config file/shape,
  does `SWITCHBOARD_SLOT` env passthrough survive.
- Effort/reasoning control: flag, config file, or none.
- Native voice/dictation surface, if any.
- Session-end signal.

Output: a new row in `family-capability-matrix.md`, same rigor as
Gemini/Kimi (mark anything unverified as unverified — don't guess).
**Do not write an `AntigravityProfile` until that spike has real answers**;
if it turns out IDE-only with no terminal/hook surface, the honest
`capabilities()` result may just be `tier="launch_only"` forever, same as
`GenericProfile` — which is a legitimate outcome, not a gap to fix.

## Workstream B — macOS install hardening (no MSC drive, no Windows)

Goal: `setup.sh` / the LaunchAgent survive re-runs, upgrades, and a
half-broken prior state without the user ever opening a terminal twice.
Reuses ideas from `auto-install-plan.md` Phase 2 (steps-as-objects,
`doctor --strict` as the consistency gate) without its cross-platform
`platform/` adapter layer, `uv` bootstrap, or MSC volume.

1. **`doctor --strict`**: add the exit-nonzero-on-any-fail variant called
   out in the original plan (today `doctor` reports but `setup.sh` doesn't
   gate on it). `setup.sh` runs it as the last step and tells the user
   exactly what failed instead of silently finishing "successfully" with a
   broken bridge.
2. **Idempotent re-run audit**: `setup.sh` already claims idempotency —
   verify it against a simulated half-state (venv exists but hooks don't;
   `agents.json` exists but LaunchAgent doesn't; old LaunchAgent present
   from a previous checkout path) rather than only the clean-machine case.
   Add this as a real test matrix, not just a doc claim.
3. **Self-healing LaunchAgent**: `KeepAlive: True` already restarts the
   bridge on crash. Add a `doctor`-driven health check path: a documented
   way to ask "is the installed bridge actually healthy right now" without
   re-running the whole installer (`switchboard doctor` already mostly
   does this — make sure it covers LaunchAgent load state, which it's
   listed as covering in `README_BRIDGE.md` but should be double-checked
   live).
4. **Migration safety**: if a second unit gets built on a Mac that already
   has an older checkout/LaunchAgent from this project's own dev history,
   re-running `setup.sh` must detect and cleanly replace it, never run two
   bridges fighting over the same serial port. This is explicitly called
   out as a risk in `auto-install-plan.md` 5.3 — worth covering even in
   the macOS-only slice since it's cheap and real (you, specifically, will
   hit this on your own machines).
5. **What's explicitly out of scope for this pass**: the `SWITCHBD` USB
   MSC drive, the signed/notarized installer, Windows support entirely.
   Those stay in `auto-install-plan.md` for when a GUI-only user is
   actually in the loop (e.g. shipping to someone who isn't you).

## Workstream C — Close the unbounded-log gap for real

Two concrete bugs found, not a redesign:

1. **`bridge.err.log` is never rotated at all.** `_rotate_bridge_out_log`
   (`host/switchboard/cli.py`) only ever touches `bridge.out.log`. Apply
   the identical rename-on-oversize treatment to `bridge.err.log` at the
   same call site.
2. **Rotation only happens at process start, not during a long-running
   session.** `_rotate_bridge_out_log()` is called once in `listen()`
   before the reconnect loop. The LaunchAgent (`KeepAlive: True`, `RunAtLoad:
   True`) is designed to stay up for weeks, so a healthy bridge that never
   crashes never rotates again after its first start. Worse: per the
   function's own docstring, renaming the path mid-run doesn't actually
   stop the *current* process's writes from landing in the renamed
   (still-open) inode — the rename only helps the *next* restart get a
   fresh file. True continuous bounding needs the bridge to stop relying
   on launchd's raw `StandardOutPath`/`StandardErrorPath` fd inheritance
   and instead manage its own output the way `trace.py` already manages
   `trace.jsonl` (a real `RotatingFileHandler`-backed stream that Python
   itself reopens on rotation). Concretely: redirect `sys.stdout`/`sys.stderr`
   in `listen()` to a small wrapper around `RotatingFileHandler` when
   running under the LaunchAgent (detect via an env var the plist sets),
   leaving interactive terminal runs untouched.
3. Add a regression test: a fake writer that pushes bytes past `max_bytes`
   across multiple simulated "ticks" (not just at start) and asserts the
   file never exceeds a bound, mirroring the existing
   `test_status_table_header_is_current`-style guard-rail tests.
4. Audit anything else that writes unboundedly: `agent_registry.json` is
   state, not a log (rewritten whole, not appended — fine). Hook curl
   calls (`curl -s --max-time 1 ... || true`) don't write locally. Confirm
   nothing else was missed with a quick `grep -rn "open(.*['\"]a['\"]" host/`.

## Workstream D — Make the settings UI findable without a terminal

Today: `switchboard settings` is a CLI subcommand that only works while
`listen` is already running, prints a URL, and opens the default browser
once. A non-terminal user (the recipient of unit #2) has no way to know
the bridge is running, healthy, or has a settings page at all.

Proposed fix: a small macOS **menu bar helper** (status item, not a Dock
app), the minimum-infra way to give this product a persistent "it's alive
and here's where to configure it" surface:

- New tiny companion script (`host/switchboard_menubar.py`), built on
  `rumps` (single new dependency, pure-Python, no Xcode project needed).
  Runs as its own LaunchAgent, separate from the bridge process — `rumps`
  needs the main thread for its `NSApplication` run loop, which doesn't
  mix cleanly with the bridge's own threads, and keeping it a separate
  process means a menu bar crash can never take the bridge down (and vice
  versa).
- Menu: current connection state (board plugged in? bridge running?
  pulled from the same loopback endpoint `doctor`/the settings page
  already use), "Open Settings", "Run Doctor" (shows `doctor`'s output in
  a simple window or notification), "Download Diagnostics" (same
  `support-bundle` the settings page already exposes), "View Logs".
- Icon reflects the single most useful glance-state: grey = bridge not
  running, color = running (optionally reusing the same status-color
  language already defined for the keys/screen, for visual consistency
  with the rest of the product).
- Installed by the same `setup.sh` pass as the bridge LaunchAgent — one
  install, two small long-running helpers, consistent with how the bridge
  itself is already installed.
- Explicitly not building: a full native app bundle, a Dock icon, or
  anything requiring code signing/notarization for this internal/gift-unit
  stage. `rumps` apps run fine unsigned for local/per-user use.

## Workstream E — 4key board: close the fab-blocking punch list

`hardware/kicad/4key/README.md`'s "Before you send these to a fab" list is
already the right checklist — this workstream is executing it, in the
order that front-loads the highest-cost-to-get-wrong items first (matches
the README's own ordering):

1. **Physical test-fit** — print the key field 1:1, test-fit a real Kailh
   hot-swap socket, a real Gateron MX switch (already have 36 on hand),
   and a real SK6812MINI. Confirms socket pads, 3.05 mm pin holes, 4 mm
   centre hole, ±5.08 mm plate holes, and the LED's 5.0 mm offset/side —
   called out as the one check that cannot be skipped.
2. **XL9555 footprint verification** — the schematic currently stands in
   `Interface_Expansion:PCF8575DBR` for the XL9555. Either confirm the
   TSSOP-24 pinout truly matches against the XL9555 datasheet, or simplify
   by just buying PCF8575s for v1 (functionally identical to the firmware,
   per the README) and dropping the open question entirely.
3. **ESP32-S3-WROOM-1 footprint + module variant** — verify pad
   pitch/thermal-pad/keepout against Espressif's datasheet; confirm N16R8
   specifically (octal PSRAM owns IO35/36/37, already reserved as
   no-connects here).
4. **I2C address collision check** — confirm 0x20 is free (it should be,
   this is a from-scratch board, not the old WonderLLM's shared bus) and
   decide now whether a second expander (for a future tier) needs A0 tied
   high for 0x21, even though 4key doesn't need it.
5. **Finish unrouted nets**: `KEY1-4` to expander I/O, the `USB_DP`/`USB_DM`
   differential pair, `CC1`/`CC2`, `~RESET`/`BOOT`, `EXP_INT` + pull-up,
   I2C pull-ups, `J2` header nets. Re-run `kicad-cli pcb drc` and
   `kicad-cli sch erc` after.
6. **LED supply budget** — confirm the actual SK6812MINI part's VDD
   range/current, size the 5 V trace widths and 22 µF bulk cap for 4 keys'
   worst-case draw (far under the 12-key tier's ~0.7 A headroom
   calculation, but still worth a real number for 4key specifically).
7. **USB-C shield-to-GND decision** — confirmed direct-to-GND in the
   generator; just make the explicit call and move on.
8. **Mounting holes vs. the real enclosure** — check the antenna-keepout-driven
   asymmetric hole pattern against the actual 3D-printed case file in
   `hardware/enclosure/`, not just the schematic.
9. **Trademark check** — "THE STICK" (4key's silkscreen nickname) in the
   relevant classes before it's on a product that leaves the house, per
   `docs/design/4key-business-plan.md`'s IP section. Confirm neither
   "Claude" nor "Codex" appear anywhere on silkscreen (README states they
   don't — verify).
10. **Order the stackup**: black soldermask / white silkscreen (already in
    the generated stackup) + ENIG, first batch sized for one working unit
    plus spares (most fabs have a 5-board minimum anyway) — confirm fab
    choice and quantity as an explicit decision before submitting.

Acceptance for this workstream: `4key/4key.kicad_pcb` passes DRC with zero
violations and zero *unintended* unrouted nets (the ratsnest items above
all closed), the open questions in the README's "what is real and what is
a stand-in" section are each resolved to a yes/no, and there's a one-line
decision record for quantity + fab house before the order is placed.

## Suggested order

A1 (Codex voice) and C (log rotation) are both same-day, low-risk fixes —
do them first, in parallel if working with subagents. B (install
hardening) and E (hardware punch list) are the two real bodies of work
this pass and don't depend on each other — run them in either order or
in parallel. D (menu bar) is small but benefits from B being done first
(nothing to surface in the menu bar until the install path it's
describing is trustworthy). A2 (Antigravity spike) is pure research and
can happen any time, independently.

## Open decisions for the owner

- A1: built-in Dictation vs. a named third-party app as the Codex
  default — proposed built-in first, fall back only if it's bad in
  practice.
- D: confirm `rumps` as an acceptable new dependency (pure Python, no
  signing needed) vs. a lighter-weight alternative (e.g. skip the menu
  bar and just have `setup.sh` auto-open the settings page once per
  login instead of a persistent icon).
- E: fab house and first-batch quantity (locked parts list already exists
  in `SWITCHBOARD_DESIGN.md`; this just needs a number).
