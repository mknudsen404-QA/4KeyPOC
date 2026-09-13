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

(`codex_micro_neokey.ino` is the pre-rename name of `firmware/neokey/neokey.ino`.)

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
  spare board.
- **New sub-finding, not in the original open-questions list:** a bare
  minimal FAT12 image only supports classic 8.3 short filenames (3-char
  extension max), which can't hold `.command` (7 chars). Solved it: added
  a Python generator, `firmware/msc_cdc_spike/gen_fat.py`, that builds a
  FAT12 image with proper **VFAT long-filename (LFN) directory entries**
  (checksum, UTF-16LE name chunks, sequence numbers per the VFAT spec)
  ahead of the 8.3 fallback entry, baking in the real
  `Install Switchboard.command` installer script as the volume's one
  file. The generated image is embedded into the sketch as
  `installer_disk.h` (a compiled-in byte array, not a flash partition).
- **MSC re-advertise-forever vs. stop-after-handshake: deferred, not
  resolved.** Punting this to the simplest option (always advertise MSC)
  for a first version, as the plan already allowed — no new information
  changes that call.
- **Validated end-to-end on the real working NeoKey board** (with
  explicit sign-off to reflash it temporarily, then reflash
  `codex_micro_neokey.ino` back after): flashed `msc_cdc_spike.ino` with
  the real installer content embedded, and confirmed all of the
  following on actual macOS hardware, not just in a compile step:
  - The board enumerates as composite CDC+MSC; a `SWITCHBD` volume
    mounts automatically in Finder.
  - The volume contains `Install Switchboard.command` with the correct
    long filename and byte-identical content to the source file (also
    cross-checked locally by mounting the generated FAT image directly
    via `hdiutil`/`diskutil` before ever touching hardware).
  - CDC keeps working normally the whole time — confirmed by reading the
    sketch's periodic serial ping directly off the port while the MSC
    volume was mounted.
  - Launching the real `.command` file off the mounted volume (Finder
    double-click equivalent) correctly detected the bridge was already
    installed on this Mac and showed the "already installed" native
    dialog, with no repo clone and no changes made — the safe branch to
    test live without disturbing the real install.
  - After testing, `codex_micro_neokey.ino` was recompiled and reflashed
    back onto the board (compile-verified first), and the
    `com.switchboard.bridge` LaunchAgent reconnected and resumed normal
    protocol traffic (agent launches, slot selection) with no errors —
    confirmed via its log. The board is back to its original working
    state.
  - Firmware upload required manually entering bootloader mode (hold
    BOOT, tap RESET, release BOOT) both times — the native-USB auto-reset
    via RTS didn't reliably drop the board into bootloader mode on this
    board/cable. Worth knowing for future firmware iteration, not a
    blocker.
  - **Real-world caution, not a firmware bug:** after reflashing
    `codex_micro_neokey.ino` back, the keys briefly stopped responding
    entirely — the ESP32 was stuck silently in `setup()`'s
    `while (!ok) { ...seesaw begin()...; delay(1000); }` retry loop,
    which never logs anything and never reaches `loop()` if the seesaw
    chip doesn't answer on I2C. This is the exact chip-level fragility
    firmware/neokey/README.md already documents, apparently triggered by
    the repeated ESP32 resets and manual BOOT-button handling during this
    spike's flash cycles. A full USB unplug/replug (which power-cycles
    the NeoKey chip itself, unlike a RESET-button tap which only resets
    the ESP32) cleared it — startup LED sweep ran and keys worked again
    immediately after. **Takeaway for later firmware work on this
    board:** expect this chip to occasionally need a full power cycle
    after heavy flash/reset cycling, and don't mistake it for a firmware
    regression before checking for the startup LED sweep.

## Relationship to the bigger picture

This is one piece of the path from "4-key NeoKey proof of concept" to the
eventual 12-key custom board with an embedded ESP32-S3 module — a
low-friction install experience matters much more once there's real
hardware to hand to other people (coworkers, etc.), not just for this
Mac Mini's own two-Mac setup.
