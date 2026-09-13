# Plug-and-Play Installer — Project Plan

**Status:** scoped, not started. This is a future project, separate from the
working 4-key NeoKey proof of concept and the `host/` bridge, both of which
already work today via `host/setup.sh`.

## The goal

First-time experience on a brand new Mac, with nothing pre-installed:

1. Plug the board in.
2. A drive/installer appears (or a permission prompt) — "install the
   Switchboard bridge?"
3. One click/double-click approves it.
4. Software downloads and installs itself, reports success or failure.
5. From that point on, forever: plug in, it just works. No terminal, no
   manual steps, ever again.

Step 5 already exists today (`host/setup.sh` installs a login LaunchAgent
that runs forever, reconnects automatically, etc.). This project is about
collapsing steps 1-4 into something close to a real "install disk"
experience, instead of "open a terminal and paste two commands."

## Why full zero-click autorun is off the table

Modern macOS (and Windows/Linux) deliberately killed USB autorun years ago
— nothing executes just because a device was plugged in, full stop. That's
an OS security floor, not a gap in this plan. So "plug in and it just runs,
no click at all" isn't achievable on any current OS. What *is* achievable,
and is a real, long-established pattern (this is exactly how USB routers,
printers, and WiFi dongles shipped installers for years): the device
presents itself as its real function *and* a tiny virtual drive containing
an installer, the drive shows up in Finder automatically, and one
double-click runs it with a permission dialog. That's the target here.

## What unlocks it

**The repo must be public.** (Already done — this project now lives at
`https://github.com/mknudsen404-QA/4KeyPOC`, renamed from
`codex-micro-switchboard` and made public specifically for this.) A
double-clicked installer stub needs to `git clone`/`curl` the real install
logic with zero authentication friction. Nothing in this repo is actually
sensitive — no API keys or credentials, just local Mac paths — so this is a
reasonable trade. Do not check in anything security-sensitive going
forward now that this assumption holds.

## Architecture

### 1. Composite USB device on the ESP32-S3 (the real engineering lift)

Today the board enumerates as a single USB CDC (serial) device. This needs
to become a **composite** device: CDC (serial, for the actual runtime
Switchboard protocol) **and** MSC (mass storage, presenting a small
read-only FAT volume) at the same time.

- Arduino-ESP32's `USB.h`/`USBCDC.h`/`USBMSC.h` classes support building
  composite native-USB devices on the ESP32-S3; this needs a real spike to
  confirm CDC + MSC can coexist cleanly with our existing NeoKey/seesaw I2C
  code, and to check how much flash a dedicated MSC partition costs against
  the board's partition table.
- The MSC backing store is a small FAT image baked into a dedicated flash
  partition, populated with exactly one file (see below). It's read-only
  from the Mac's perspective in the simplest version — no need to support
  writing back to the device.

**Risk:** this is new firmware territory (not just "add a library call"),
so it should be prototyped on its own, away from the working NeoKey
firmware, before merging in. Do not risk the already-working 4-key proof of
concept board on this — use a second board or a separate branch/sketch.

### 2. The installer stub (what's on that virtual drive)

One file: a macOS `.command` script (Finder runs `.command` files directly
in Terminal on double-click — no separate installer framework needed).

Its logic:

```
1. Check whether the bridge is already installed (e.g. does
   ~/Library/LaunchAgents/com.switchboard.bridge.plist exist?).
   - If yes: show a "already installed, you're all set" dialog and exit.
     (Handles the case where someone double-clicks it again later, or a
     second Mac already has an older Switchboard board's stub.)
2. If not installed: show a native permission dialog
   (`osascript -e 'display dialog ...'`) — "Install the Switchboard bridge
   now?" Yes/Cancel.
3. On Yes: `git clone https://github.com/mknudsen404-QA/4KeyPOC.git`
   (public, no auth needed) into a sensible default location, then
   `cd 4KeyPOC/host && ./setup.sh`.
4. Show a final success or failure dialog (`osascript`) summarizing what
   happened, pointing at ~/Library/Logs/Switchboard for details on failure.
```

Keep this stub as thin as possible — it should just bootstrap into the
real `setup.sh`, not duplicate its logic. That way updating the actual
install steps later only means editing `setup.sh` in the repo, not
re-flashing every board with a new stub.

### 3. Testing

- A truly fresh Mac (or a fresh admin user account on an existing Mac) with
  no prior Switchboard install, no repo cloned, nothing.
- Re-plugging after install (should no-op cleanly, not re-clone or
  duplicate the LaunchAgent).
- What happens on a Mac where `git` isn't installed yet (Xcode Command
  Line Tools prompt) — decide whether the stub should detect this and
  guide the user, or just let git's own "install command line tools?"
  system prompt handle it.
- Windows/Linux: out of scope. The `.command` file will just look like an
  inert text file there; the drive itself is harmless clutter. This whole
  project targets macOS only, matching the rest of the bridge.

## Open questions to resolve during the spike

- Exact flash/partition budget for an MSC volume on this board's ESP32-S3
  (need enough free space with the current partition table).
- Whether the Arduino-ESP32 core version already in use supports composite
  CDC+MSC cleanly, or needs an update.
- Whether the drive should re-appear/re-mount on every plug-in forever
  (simplest, but a little bit of permanent clutter once installed) or
  whether there's a clean way to have the firmware stop advertising MSC
  once it detects the bridge round-tripping real protocol traffic (fancier,
  not necessary for a first version).

### Spike findings (resolved 2026-09-12)

- **Flash/partition budget: a non-issue.** Read the working NeoKey board's
  flash chip directly with esptool (`flash-id`, read-only — no write, no
  firmware change): ESP32-S3 with **16MB flash / 8MB PSRAM**. The
  `esp32:esp32:esp32s3` board already ships 16M partition schemes with
  multi-megabyte FAT partitions (e.g. `app3M_fat9M_16MB` = 3MB
  app / 9.9MB FATFS). More importantly, **we don't need a dedicated flash
  partition at all**: the real installer payload is a few KB of shell
  script, comfortably inside SRAM budget, so the MSC backing store can be
  a small static RAM-resident FAT image (like Arduino-ESP32's own bundled
  `USBMSC.ino` example does) instead of a flash partition. This sidesteps
  partition-table changes — and any risk of colliding with
  `codex_micro_neokey.ino`'s partition layout — entirely. Confirmed via a
  compiled spike (see below): 62,832 bytes (19%) of the ~327KB dynamic
  memory budget used with a full composite CDC+MSC stack running, leaving
  plenty of headroom even after adding NeoKey/seesaw/ArduinoJson state.
- **Composite CDC+MSC support: yes, cleanly, no core update needed.** The
  currently-installed Arduino-ESP32 core (**3.3.11**) has first-class
  support: `boards.txt` even exposes a board-menu-level `MSCOnBoot` option
  alongside the `CDCOnBoot` one this project already uses, and the core
  ships `USBMSC.h`/`USBMSC.cpp` plus a bundled composite example
  (`libraries/USB/examples/USBMSC/USBMSC.ino`) demonstrating CDC (via
  `Serial`, in native USB-OTG mode) and MSC coexisting via a shared
  `USB.begin()`. Wrote a standalone spike sketch,
  `firmware/msc_cdc_spike/msc_cdc_spike.ino` — deliberately separate from
  `codex_micro_neokey.ino`, no NeoKey/seesaw code, safe to flash to a
  spare board — and compile-verified it (no upload) against this exact
  core with `arduino-cli compile --fqbn
  esp32:esp32:esp32s3:USBMode=default,CDCOnBoot=cdc,FlashSize=16M,PartitionScheme=app3M_fat9M_16MB`:
  builds clean, 353,446 bytes (11%) of app partition. Not yet flashed to
  physical hardware — that needs a second board (see below) before this
  spike can be called fully validated end-to-end.
- **New sub-finding, not in the original open-questions list:** a bare
  minimal FAT12 image only supports classic 8.3 short filenames (3-char
  extension max), which can't hold `.command` (7 chars). The real
  installer volume will need **VFAT long-filename (LFN) directory
  entries** (extra 0x0F-attribute directory records ahead of the normal
  8.3 entry) to expose a file actually named e.g.
  `Install Switchboard.command`. This is a well-documented, mechanical
  extension to the FAT12 image-building code already spiked — not a
  blocker, just scoped into the next step (building the real installer
  stub) rather than this composite-USB spike.
- **MSC re-advertise-forever vs. stop-after-handshake: deferred, not
  resolved.** Punting this to the simplest option (always advertise MSC)
  for a first version, as the plan already allowed — no new information
  changes that call.
- **Not yet done:** flashing `msc_cdc_spike.ino` to real hardware and
  confirming a Mac actually mounts the volume and Finder shows the file
  correctly. This requires a second ESP32-S3 board (or explicit sign-off
  to briefly reflash the working NeoKey board, with a plan to reflash
  `codex_micro_neokey.ino` back immediately after) — not done yet pending
  that decision.

## Relationship to the bigger picture

This is one piece of the path from "4-key NeoKey proof of concept" to the
eventual 12-key custom board with an embedded ESP32-S3 module — a
low-friction install experience matters much more once there's real
hardware to hand to other people (coworkers, etc.), not just for this
Mac Mini's own two-Mac setup.
