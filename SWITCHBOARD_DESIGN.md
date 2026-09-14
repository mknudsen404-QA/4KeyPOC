# Switchboard Design Decisions

Last updated: 2026-09-06

## Product Name

The device is named **Switchboard**. The earlier name, Codex Micro Console, implied a Codex-only tool; the device is agent-agnostic by design (Codex, Claude, Cursor, or any other CLI agent can occupy a slot), and "Switchboard" better matches what it actually does: a physical panel that routes deliberate command intents to whichever agent is live, like an operator's switchboard routing calls. The firmware project is now named `switchboard` (CMake `project(switchboard)`; the built binary is `switchboard.bin`).

The redesigned Agents screen (see "Illuminated Keys And Status" and the near-term firmware plan) no longer renders an on-screen title — the terminal-style layout reclaims that vertical space for agent/status/activity content, matching the "screen answers three questions" philosophy in Core Concept. If the device gets a physical bezel later, the brand mark belongs there instead of on-screen.

## Current Working Baseline

The current firmware is based on Hiwonder `06_lvgl_font` and runs as an ESP-IDF project on the ESP32-S3 board.

What is already working:

- Built-in 320 x 240 ST7789 display initializes and renders LVGL UI.
- FT62xx touch controller initializes.
- XL9555 key reads work for KEY1 through KEY4.
- The flashed screen now models four fake agent slots and six future command slots.
- KEY1 and KEY2 select previous/next agent.
- KEY3 cycles/applies reasoning effort for the selected agent.
- KEY4 acts as hold-to-talk microphone start/stop.
- USB serial emits JSON-lines style events for agent selection, reasoning effort, and voice hold/release.
- Firmware builds and flashes through ESP-IDF 5.4.3.
- Firmware now declares the board's detected 16 MB flash size.
- The board receives bridge `agent.update` messages over USB Serial/JTAG and acknowledges them with `agent.update.ack`. This was verified by syncing Agent 1 as `Maestro / working / bridge sync test` and receiving `Board applied update for Agent 1`.
- The Agents screen was reskinned to a dark terminal look: monospace UNSCII 16 body font, left-aligned layout, a colored status dot next to the status word, a tappable 4-cell agent strip (touch-select an agent directly, previewing the planned illuminated NeoKey behavior), and a single bottom hint bar that shows the key legend and flashes green/red for feedback instead of a separate always-on command row. Visual design only — the state machine, host-bridge events, and key mapping are unchanged. Prototyped first as a clickable mockup before porting to LVGL.

Current board port used during bring-up:

```text
/dev/cu.usbmodem2301
```

## Current Bridge Boundary

The bridge can launch/register a slot and push that slot's registered status to the screen. Pressing an agent key can therefore cause the bridge to launch the configured CLI and immediately update the board with the launched state.

The bridge does not yet observe the live contents of a Codex or Claude terminal session. If Codex asks for access, approval, or another response inside its own terminal, the bridge currently does not know that happened. The next host-side milestone is to launch agents under a bridge-owned session/PTY or another observable session wrapper so the bridge can classify live states such as thinking, waiting for approval, blocked, done, or needs input.

## Status Auto-Detection Plan

Goal: make status/activity (thinking, working, waiting, needs_input, blocked, done) reflect what the CLI is actually doing, instead of only ever being set by hand.

**Superseded first attempt (log-tail pattern matching).** The original plan was to tee session output via `script -q` and pattern-match the ANSI-stripped tail. Implemented and tested live, but a real captured log showed the flaw: Codex's TUI is a full-screen, cursor-positioned alternate-screen interface (`\e[row;colH` redraws, box-drawing glyphs), not linear scrolling text — naive ANSI-stripping cannot turn that into reliably matchable text; a correct version would need a real terminal emulator (`pyte`) to reconstruct the rendered screen first. **As of Phase 0 this has been removed entirely** (`script -q`, `host/logs/`, `poll_slot_logs`, `classify_activity` are all gone) — hooks are now the only status source. A closed tab/window (which fires no lifecycle hook at all) is instead caught by process-liveness probing (`probe_liveness`/`reconcile_liveness`), not log-tailing.

**Current architecture: real CLI lifecycle hooks, not screen-scraping.** Both Claude Code (`~/.claude/settings.json`) and Codex CLI (`$CODEX_HOME/hooks.json`) expose documented lifecycle hooks (`SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Notification`/`PermissionRequest`, `Stop`, `SessionEnd`) that run an arbitrary shell command on each transition. This is validated by [OpenMicro](https://github.com/stephenleo/OpenMicro) (128-star real project solving the same problem for game controllers), whose hook-installer code was used as the reference implementation. Mechanism:

1. `switchboard_bridge.py install-hooks` merges a `curl`-based hook command into both CLIs' hook config (idempotent, marker-based so re-running or another tool's installer never clobbers unrelated hooks; verified against a real `~/.claude/settings.json` that already had unrelated settings — those were preserved).
2. Each launched session gets `export SWITCHBOARD_SLOT=N` before exec (same trick as OpenMicro's `OPENMICRO_INSTANCE_ID`), so its hooks can tag which slot they belong to via an `X-Switchboard-Slot` header.
3. The hook command is `curl -s --max-time 1 ... || true` — fire-and-forget, so it no-ops harmlessly whenever the bridge isn't running and never blocks the CLI.
4. The bridge runs a small `ThreadingHTTPServer` on `127.0.0.1:8877` (`start_hook_server`) that maps `(family, event)` → status via `CLAUDE_HOOK_STATUS`/`CODEX_HOOK_STATUS` and pushes `agent.update` on change. Proven end-to-end with synthetic `curl` POSTs against an isolated registry: `UserPromptSubmit` → `working`, `Stop` → `done`, exactly as expected.

Known limitations: hooks only take effect for sessions started *after* `install-hooks` runs (already-running sessions, e.g. one launched before this was wired in, won't call the new hooks until restarted). The event→status mapping is a reasonable first guess, not yet calibrated against a full real session — `PreToolUse` only fires for `AskUserQuestion` specifically (not all tool calls), and Codex has no dedicated error/`blocked` hook signal (OpenMicro notes the same gap: "⚠️ Notification-text matching" for Claude, "— No error hook signal" for Codex), so `blocked` is currently unreachable for either family — there is no fallback log classifier anymore, by design (see above).

For the first external switch test, use RXD0 from the actual board UART connector area as a simple input. Firmware maps External Agent Key 1 to GPIO44/RXD0. Wire a momentary switch between RXD0 and GND only. Do not use 5V, TXD pins, PWM/servo connectors, or I2C SCL/SDA for the bare switch test. The NeoKey/Qwiic I2C path remains the preferred external key-bank plan.

## Verified Hardware Facts

The real board is a Hiwonder **WonderLLM** (schematic title "ESP32S3 Large Model Board," ESP32-S3-WROOM-1-N16R8, 16 MB flash / 8 MB PSRAM), not just a display demo board. It also carries a camera FPC connector, ES8311 audio codec, IMU, EEPROM, servo bus, TF card slot, buzzer, and an ambient-light/IR/proximity sensor that the original bring-up docs never mentioned. Hiwonder does not publish a pin-by-pin GPIO map for custom firmware: their `docs.hiwonder.com/projects/WonderLLM` documentation and Google Drive resources cover using the stock firmware over a UART/JSON "MCP tool" protocol, and the Arduino board package they distribute (`esp32_package_2.0.12_arduinome.exe`) turned out to be the stock public Espressif ESP32 Arduino core with no Hiwonder-specific pin file — a variant named `esp32s3camlcd` exists in it but is for an unrelated generic 480x320 parallel-TFT board, not this one. Because no vendor GPIO map exists, every pin below is taken directly from this project's own flashed, working source (`components/BSP/XL9555/xl9555.h`, `components/BSP/IIC/iic.h`) cross-checked against the schematic — not from a datasheet.

| Signal | Pin / Location | Source |
| --- | --- | --- |
| XL9555 expander interrupt | GPIO46 | `xl9555.h` |
| Primary I2C bus (touch, XL9555, EEPROM) | SDA=GPIO38, SCL=GPIO48 | `iic.h` (`IIC_*`) |
| Second I2C bus — defined in code, not yet used by any driver | SDA=GPIO4, SCL=GPIO5 | `iic.h` (`IIC1_*`) |
| LCD chip-select | XL9555 expander bit `0x2000` — **not a raw GPIO** | `xl9555.h` (`LCD_CS_IO`) |
| LCD backlight | XL9555 expander bit `0x4000` | `xl9555.h` (`BACKLIGHT_IO`) |
| KEY1-KEY4 | XL9555 expander bits `0x0010`-`0x0080` | `xl9555.h` |
| External Agent Key 1 (first outboard switch test) | GPIO44 (also the chip's default UART0 RXD0 pin), active-low, internal pull-up | `lv_mainstart.c` |
| Mic/I2S (unwired in firmware so far, but pins confirmed against schematic) | MCLK=45, BCLK=39, LRCK=41, DOUT=42, DIN=40 | Schematic sheet 2 ("Encoder/IIS") |

Correction: the earlier README note that "the vendor LED sample used GPIO2, which is also LCD chip select" does not apply to this firmware's actual pin plan — LCD_CS lives on the XL9555 expander, not on a raw GPIO, so there is no GPIO2 contention for status LEDs on this design. That conflict was specific to a different vendor sample, not a real constraint here. It should still be re-verified before wiring status LEDs, but it is not a known blocker.

Firmware risk carried over from this: `lv_mainstart.c` now sends/receives the JSON host-bridge protocol over the native USB-Serial-JTAG peripheral (`usb_serial_jtag_write_bytes`/`read_bytes`). If the ESP-IDF console (`printf`/`ESP_LOG`) is also routed through that same USB-Serial-JTAG channel — which was the tested default per the original README — any stray debug log line will interleave into the same byte stream the Mac-side bridge is parsing as JSON-lines, corrupting it. Confirm the console output channel in `idf.py menuconfig` (Component config -> ESP System Settings -> Channel for console output) and either move it to UART0 or ensure no `printf`/`ESP_LOGx` calls run on the same path as the host-bridge writes.

Rotary encoder guidance now that real pins are known:

- If the encoder module has an I2C breakout (a small PCB with a chip, not bare CLK/DT/SW legs), it can plug into the second I2C bus (GPIO4 SDA / GPIO5 SCL) that's already defined in `iic.h` but unused by any driver — no new GPIO wiring needed, just a driver/init call.
- If it's a bare mechanical encoder (KY-040-style, plain CLK/DT/SW pins), it needs 2-3 raw GPIOs with interrupts. Avoid GPIO0, GPIO3, GPIO45, and GPIO46 (ESP32-S3 strapping pins — using them as plain inputs risks interfering with boot-mode selection) and the pins already spoken for above (4, 5, 38, 44, 46, 48). Confirm candidate pins against the physical header's silkscreen with a multimeter before soldering — the schematic's text extraction does not reliably preserve left-to-right pin order, so it can tell you which signals exist but not which physical pin position they sit at.

## Project And Reference Locations

Primary live firmware project:

```text
/Users/matthewknudsen/Documents/Codex-Micro-Matt/Codex_Micro_Display
```

Design decisions:

```text
/Users/matthewknudsen/Documents/Codex-Micro-Matt/Codex_Micro_Display/CODEX_MICRO_CONSOLE_DESIGN.md
```

Session deliverable copies:

```text
/Users/matthewknudsen/Documents/Codex/2026-09-05/referenced-chatgpt-conversation-this-is-an/outputs/CODEX_MICRO_CONSOLE_DESIGN.md
/Users/matthewknudsen/Documents/Codex/2026-09-05/referenced-chatgpt-conversation-this-is-an/outputs/README_CODEX_MICRO.md
```

Stable copied PDF references:

```text
/Users/matthewknudsen/Documents/Codex-Micro-Matt/Codex_Micro_Display/docs/reference/5. LVGL Development Tutorial.pdf
/Users/matthewknudsen/Documents/Codex-Micro-Matt/Codex_Micro_Display/docs/reference/6. ESP32-S3 Development Board Diagram_V1.1.pdf
```

`6. ESP32-S3 Development Board Diagram_V1.1.pdf` is the real 4-page schematic ("ESP32S3 Large Model Board") for the exact board in this project and is the primary hardware ground truth alongside the flashed firmware source. Do not confuse it with `/Users/matthewknudsen/Downloads/ESP32-S3-All-Data/3. Datasheet/4.SCH_ESP32S3 Schematic Diagram.pdf` — that OneDrive folder documents a different Hiwonder product (a bare "ESP32S3-CAM" vision module) and does not describe this board's display, expander, or connector pinout. Hiwonder's official WonderLLM documentation and Google Drive (`docs.hiwonder.com/projects/WonderLLM`) were also checked and do not publish a GPIO pin map for custom firmware; see "Verified Hardware Facts" above for what is actually confirmed.

Original extracted Hiwonder source-code examples:

```text
/Users/matthewknudsen/Documents/Codex-Micro-Matt/Source Code
```

Key source-code examples:

| Example | Path | Why It Matters |
| --- | --- | --- |
| LCD/LVGL font baseline | `/Users/matthewknudsen/Documents/Codex-Micro-Matt/Source Code/06_lvgl_font` | Base for the current display firmware |
| PC LVGL examples | `/Users/matthewknudsen/Documents/Codex-Micro-Matt/Source Code/03_PC_lvgl` | Desktop LVGL reference |
| Touch/mouse LVGL example | `/Users/matthewknudsen/Documents/Codex-Micro-Matt/Source Code/04_lvgl_add_mouse` | Input-device reference |
| Filesystem LVGL example | `/Users/matthewknudsen/Documents/Codex-Micro-Matt/Source Code/05_lvgl_fs_use` | Storage/filesystem reference |
| SPI flash font example | `/Users/matthewknudsen/Documents/Codex-Micro-Matt/Source Code/07_lvgl_spiflash_font` | Larger font/storage reference |
| TTF font example | `/Users/matthewknudsen/Documents/Codex-Micro-Matt/Source Code/08_lvgl_ttf_font` | Future typography reference |
| Object/widget example | `/Users/matthewknudsen/Documents/Codex-Micro-Matt/Source Code/09_lvgl_obj` | UI widget reference |
| Arc example | `/Users/matthewknudsen/Documents/Codex-Micro-Matt/Source Code/10_lvgl_arc` | Useful for gauges/dial visuals |
| Bootloader/full demo | `/Users/matthewknudsen/Documents/Codex-Micro-Matt/Source Code/11_bootloader` | Audio, camera, richer demo app, and I2S/ES8311 references |
| Paint demo | `/Users/matthewknudsen/Documents/Codex-Micro-Matt/Source Code/12_lvgl_paint` | Touch drawing and richer interaction reference |

Original Arduino examples:

```text
/Users/matthewknudsen/Documents/Codex-Micro-Matt/Arduino Program
```

Key Arduino examples:

| Example | Path | Why It Matters |
| --- | --- | --- |
| XL9555 keys | `/Users/matthewknudsen/Documents/Codex-Micro-Matt/Arduino Program/XL9555_key` | Proven KEY1-KEY4 reads and expander definitions |
| LED | `/Users/matthewknudsen/Documents/Codex-Micro-Matt/Arduino Program/LED` | Original simple LED reference |
| Key | `/Users/matthewknudsen/Documents/Codex-Micro-Matt/Arduino Program/key` | Basic onboard key example |
| Key interrupt | `/Users/matthewknudsen/Documents/Codex-Micro-Matt/Arduino Program/key_EXTI` | Interrupt-based key reference |
| SPI SD card | `/Users/matthewknudsen/Documents/Codex-Micro-Matt/Arduino Program/SPI_SDCARD` | Storage plus reused XL9555/key/LED helpers |
| UART | `/Users/matthewknudsen/Documents/Codex-Micro-Matt/Arduino Program/uart` | Serial communication reference |

Original archives:

```text
/Users/matthewknudsen/Documents/Codex-Micro-Matt/Arduino Program.rar
/Users/matthewknudsen/Documents/Codex-Micro-Matt/Source Code.rar
```

Conversation-attached reference files were also present in temporary preview folders. The two important PDFs have been copied into `docs/reference` so future work does not depend on temporary paths.

Claude review notes:

```text
/Users/matthewknudsen/Documents/Codex-Micro-Matt/Codex_Micro_Display/CLAUDE_REVIEW_BRIEF.md
/Users/matthewknudsen/Documents/Codex-Micro-Matt/Codex_Micro_Display/CLAUDE_FEEDBACK.md
```

Host bridge:

```text
/Users/matthewknudsen/Documents/Codex-Micro-Matt/Codex_Micro_Display/host/switchboard_bridge.py
/Users/matthewknudsen/Documents/Codex-Micro-Matt/Codex_Micro_Display/host/install_bridge_launch_agent.py
/Users/matthewknudsen/Documents/Codex-Micro-Matt/Codex_Micro_Display/host/README_BRIDGE.md
/Users/matthewknudsen/Documents/Codex-Micro-Matt/Codex_Micro_Display/host/EXTERNAL_KEY_SPIKE.md
/Users/matthewknudsen/Documents/Codex-Micro-Matt/Codex_Micro_Display/host/agents.example.json
```

## Core Concept

Switchboard should be a physical control surface for AI coding-agent work, not a tiny general-purpose keyboard.

The screen answers three questions at a glance:

- Which agent or task is selected?
- What is that agent doing right now?
- What command context am I in?

The controls send deliberate command intents:

- Adjust reasoning effort.
- Start microphone input.
- Jump directly to one of the available running CLI agent slots.
- Approve a plan.
- Request a code review.
- Run a custom slash command.

Primary design direction: the console should expose one physical illuminated key per agent slot, plus a separate command bank. The agent key's color and animation show the agent's status, and pressing the key selects that agent on the screen.

Hardware status: v1 should assume four agent slots because four is a good first control surface size, not because the board is limited to four total buttons. The current validated hardware has four onboard keys, display, touch, and USB serial. The intended physical console can still add external controls: four illuminated agent keys plus six command keys.

Decision: the onboard KEY1-KEY4 buttons are prototype/dev controls. The final external Gateron or illuminated keys should be wired as new inputs rather than trying to mechanically or electrically press the built-in buttons.

## Layer Model

The first real layer set should be:

| Layer | Purpose | Primary Use |
| --- | --- | --- |
| Agents | Navigate active agents/tasks and inspect status | Day-to-day orchestration |
| Commands | Approve, review, continue, stop, and run slash commands | Fast actions |
| System | Brightness, connection, battery/power, diagnostics | Device health |

The current firmware still shows the older placeholder layers:

```text
CODEX
QA
SYSTEM
```

Decision: keep the current flashed behavior as the v0 proof-of-life, then replace it with the layer model above in the next firmware pass.

Decision from Claude review: Voice should not be a peer layer in v1. Microphone input is a cross-layer action, with KEY4 hold-to-talk available wherever it is useful. A later Voice screen can exist for transcript review or audio settings if the workflow proves it needs one.

## Rotary Dial

The rotary dial should adjust the selected agent's reasoning effort.

Initial reasoning-effort scale:

| Dial Position | Label | Intended Meaning |
| --- | --- | --- |
| 0 | Low | Fast, cheap, lightweight work |
| 1 | Medium | Default coding and review work |
| 2 | High | More careful planning, debugging, or architecture |
| 3 | XHigh | Deep implementation and difficult reasoning |
| 4 | Max | Highest-effort mode for hard problems |

Decision: rotating changes the effort preview immediately on screen. Pressing the dial, if the encoder has a press switch, commits the change. If the dial has no press switch, KEY3 on the Agents layer commits it.

Open hardware check: identify the rotary encoder pins and whether it has an integrated press switch. See "Verified Hardware Facts" below for which GPIOs/I2C bus are actually free versus already claimed by the display, touch, expander, and mic — no vendor pin map exists for this board, so candidate pins must be checked against that list plus physical continuity on the board itself, not guessed.

## Visual Identity

Switchboard should ship with tiny 8-bit character art for both thinking effort and agent family identity.

Decision: keep 8-bit character art as optional identity and delight, not as the primary status system.

- A small character/avatar shows whether the selected agent is Codex or Claude.
- A separate "thought meter" can show reasoning effort if it remains legible.
- Important status moves to illuminated keys/LEDs and text on the screen.

Initial 8-bit identity sprites:

```text
CODEX
..####..
.#....#.
.#.##.#.
.#....#.
.#.##.#.
.#....#.
..####..
...##...

CLAUDE
..####..
.#....#.
.#.##.#.
.#....#.
.#.####.
.#....#.
..####..
..#..#..
```

Initial effort sprites:

```text
LOW
...##...
..#..#..
..#..#..
...##...
........
....#...
........
........

MEDIUM
...##...
..#..#..
..#..#..
...##...
........
...###..
.....#..
........

HIGH
...##...
..#..#..
..#..#..
...##...
........
..#####.
.....#..
..###...

XHIGH
...##...
..#..#..
..#..#..
...##...
..#..#..
...##...
..####..
.#....#.

MAX
..#..#..
...##...
.######.
#..##..#
...##...
..####..
.#.##.#.
#..##..#
```

Rendering decision: if used, start with simple monochrome 8 x 8 bitmap sprites in firmware. Do not status-tint the whole sprite, because that asks a tiny glyph to carry too much information. Use a fixed family accent for Codex or Claude and put status color on a separate key light, LED, strip, or screen indicator.

Possible effort-color mapping:

| Effort | Color | Feel |
| --- | --- | --- |
| Low | White | Awake but light |
| Medium | Cyan | Focused |
| High | Blue | Deep work |
| XHigh | Purple | Heavy reasoning |
| Max | Magenta/white pulse | Full send |

Claude/Codex distinction:

| Agent Family | Sprite Accent | Notes |
| --- | --- | --- |
| Codex | Green or cyan eye pixels | Code/workbench identity |
| Claude | Amber or soft purple eye pixels | Review/critique identity |

Open implementation choice: LVGL labels can draw these as text first, but true pixel sprites will look sharper and more intentional on the ST7789 screen.

## Illuminated Keys And Status

The preferred direction is to use keys that light up with color to indicate agent or command status.

Decision: physical key lighting becomes the primary glanceable status surface. The screen remains the source of text context: agent name, current activity, status word, and reasoning effort.

**Status update (screen now temporary):** the display currently in hand tops out at 140×280 (a smartwatch-class panel), too narrow for the terminal-style layout built so far, and is a stopgap until a proper 320×240/2.0" panel arrives (~October). Given that, key-light color and intensity is now the *primary* status channel by necessity, not just by preference — without a screen worth building a real UI for in the meantime, the agent keys' color/intensity are the only glanceable status signal available at all. This isn't a new decision so much as this section's original intent actually taking effect sooner than planned.

**NeoKey implementation plan (not yet built — no NeoKey hardware in hand yet):** each NeoKey 1x4 QT's Seesaw chip drives its 4 switches and 4 NeoPixels over the same I2C bus, so per-key RGB + brightness is a firmware I2C write, not a hardware question. This reuses two things already built for the screen rather than inventing a new status language:

- **Color** — the exact mapping in "Agent Status Colors" below (EMPTY/IDLE/THINKING/WORKING/WAITING/NEEDS_INPUT/BLOCKED/DONE → color). No new palette needed.
- **Intensity/animation** — the same `status_is_busy()` split already implemented for the screen's thinking-dots animation (THINKING/WORKING/WAITING = busy) maps directly to pulsing brightness on the LED instead of chasing dots on a screen; everything else sits at a static brightness matching its color.

Firmware gap this creates: today's `agent.update` handling only touches LVGL widgets. A NeoKey driver needs its own I2C write path (separate from the XL9555/touch bus) to push color+brightness per key whenever `refresh_agent_keys()`-equivalent logic runs. Not started; tracked here so it isn't lost once the boards arrive.

Target agent bank for v1:

| Physical Control | Meaning |
| --- | --- |
| Agent Key 1 | Select/focus CLI agent slot 1 |
| Agent Key 2 | Select/focus CLI agent slot 2 |
| Agent Key 3 | Select/focus CLI agent slot 3 |
| Agent Key 4 | Select/focus CLI agent slot 4 |

Each future agent key is both an input and a status light. The screen shows details for the selected key. The keys show the whole active-agent field at a glance.

Target command bank for v1:

| Physical Control | Meaning |
| --- | --- |
| Command Key 1 | Approve plan, hold to confirm |
| Command Key 2 | Request code review |
| Command Key 3 | Continue or run selected command, hold to confirm if mutating |
| Command Key 4 | Hold-to-talk microphone |
| Command Key 5 | Custom slash command |
| Command Key 6 | Back, cancel, stop, or command-context escape |

Current bring-up behavior: the onboard KEY1-KEY4 controls can temporarily represent either the four agent slots or four command actions while firmware is developed. They are not the final input count and should not constrain the external console layout.

Status should be key-light-first for glanceability and text-confirmed on screen:

```text
WORKING      + orange pulse
NEEDS INPUT  + yellow fast flash
BLOCKED      + red solid
DONE         + green or blue solid
```

Proposed lit-key meanings:

| Key Light | Meaning In Agents View |
| --- | --- |
| Agent key | Shows that agent's current status color |
| Selected agent key | Same status color, brighter or with a thin screen highlight |
| Microphone key | Off when idle, red while recording, cyan while transcribing |
| Confirm/run key | Green when safe/ready, yellow when confirmation is needed |

Status light behavior:

| Light Behavior | Meaning |
| --- | --- |
| Off | Empty slot or disconnected agent |
| Dim solid | Idle/available |
| Slow pulse | Working, thinking, editing, or testing |
| Fast flash | Needs user input or confirmation |
| Steady bright | Done and ready to inspect |
| Short green flash | Command accepted or completed |
| Short red flash | Command rejected or failed |

Hardware direction:

- Prefer ten external controls for the intended v1 console: four illuminated agent keys and six command keys. The first bench test should still be small: one external switch plus one status LED, then scale after the wiring and firmware model are proven.
- Do not try to trigger the built-in KEY1-KEY4 buttons from external switches. Avoid soldering across the onboard button pads unless there is a specific debugging reason. External keys should connect to spare ESP32 GPIO pins, a keypad matrix, or an external GPIO expander/key scanner.
- Preferred expansion direction: use an external I2C GPIO expander or keypad scanner for button inputs, plus WS2812/SK6812-style addressable RGB LEDs for per-key lighting. That keeps the key input circuit and key lighting circuit separate and avoids spending one GPIO per light.
- Keep the current onboard KEY1-KEY4 as firmware bring-up controls until the external key hardware is chosen.
- The earlier GPIO2/LCD-chip-select LED conflict does not apply to this firmware's actual pin plan (see Verified Hardware Facts above: LCD_CS is on the XL9555 expander, not a raw GPIO). Still confirm no other status-LED GPIO is already claimed before wiring, but GPIO2 specifically is not a known blocker here.

## Microphone Key

There should be a dedicated microphone key.

Decision: KEY4 becomes the microphone key on the Agents layer.

Behavior:

- Tap: start or stop microphone input.
- Hold: push-to-talk while held.
- Screen shows listening, transcribing, or unavailable state.
- Host bridge receives a `voice.toggle` or `voice.hold` event.

Decision from Claude review: hold-to-talk should be available from Agents and Commands. Tap-to-toggle can remain available, but hold-to-talk is the safest first behavior because releasing the key clearly stops capture.

Open hardware check: confirm the microphone/audio path. Hiwonder's `11_bootloader` project includes ES8311/I2S audio references with these likely pins:

| Signal | GPIO |
| --- | --- |
| MCLK | 45 |
| BCLK | 39 |
| LRCK/WS | 41 |
| DOUT, ESP32 to codec | 42 |
| DIN, codec to ESP32 | 40 |

Those pins should be verified against the board schematic or a minimal audio capture test before building voice features.

## Agent Navigation

The console should support four running CLI agents at once for v1.

Decision: agent navigation should be direct selection by illuminated key. The intended v1 product has four external agent keys. The current four-button board maps naturally to four stable agent slots during bring-up, but those onboard buttons are a temporary stand-in.

The selected agent card should show:

- Slot number, such as `AGENT 3/4`.
- Agent name or task title.
- Current status color.
- Short activity line, such as `editing lv_mainstart.c`, `waiting for tests`, or `needs approval`.
- Reasoning effort.
- Last update age.

Agent slot behavior:

- Press an agent key: select that agent and show details on screen.
- Hold an agent key: open/focus that CLI session on the host, if the bridge supports it.
- Empty slot key: off, press can offer to attach or spawn a new agent later.
- Agent key color always follows the agent status, even when another layer is active.

Future rotary behavior:

- Rotate: adjust reasoning effort for selected agent.
- Press: apply effort.
- Long press: open an agent picker or pin current agent.

## Agent Status Colors

The screen, illuminated keys, and WS2812 LEDs use the same status language.
Implemented in `firmware/neokey/led_model.h`; as of Phase 2.5 the table
below is generated, not hand-maintained — its single source of truth is
`host/switchboard/status_table.py`'s `STATUSES`, which also generates
`firmware/neokey/status_table.h` (the `Status` enum, `colorForStatus`, and
the static pulse table `led_model.h` builds on). Edit that one table and
regenerate rather than editing this section or the header by hand — see
`host/README_BRIDGE.md`'s "Adding a status".

| Status | Color | Meaning |
| --- | --- | --- |
| Empty | Off | No agent assigned to this slot |
| Launched / Idle | Soft white, solid | Session running but not mid-turn |
| Thinking / Working (busy) | White -> blue -> magenta ramp, pulsing faster over the turn | Agent is actively working; color/pulse both escalate over the first ~5 minutes of the current turn, then hold at magenta/fast |
| Waiting / Needs Input | Yellow, fast pulse (500 ms) | User decision or clarification required |
| Blocked | Red, solid | Agent cannot proceed without intervention |
| Done | Green, solid | Task/turn completed; clears to Idle the moment the key is pressed or the next prompt starts (Phase 2.2) |
| Unknown | Amber, slow pulse (3000 ms) | The bridge hasn't heard from this slot recently — not a status the agent reports, a "we're not sure" signal from the board/bridge relationship itself |

The busy ramp (`busyColor()` in `led_model.h`) measures the **current
turn**, resetting on `UserPromptSubmit` — a long-running turn escalates, but
starting a new prompt starts the ramp over rather than continuing to climb
from a previous turn's color. Non-selected keys are dimmed to 40% of their
color (floored so a dim status never disappears entirely), selected keys are
shown at full intensity.

### The `liveness` field (Phase 2.3)

`agent.update` carries an optional `liveness` field: `"alive"`, `"unknown"`,
or omitted entirely (no news either way). It's orthogonal to `status` — a
slot can report `"working"` and `"unknown"` liveness at the same time if
the bridge can no longer confirm the process behind it is actually there
(the terminal tab's `ps` probe came back inconclusive twice in a row).
When `liveness: "unknown"` arrives, the key overrides *whatever* its
current `status` would otherwise render and shows Unknown's amber pulse
instead — the LED may be lying about `status`, so don't trust it until a
confirmed `liveness: "alive"` (or a fresh hook-driven update, which only
fires if the process is alive) clears it. This is Bridge-side bookkeeping
only (`ReducerState.unknown_probes` in `reducer.py`); the firmware just
renders whatever `liveness` value it's told (`slotUncertain[]` in
`neokey.ino`, the `uncertain` parameter on `renderKey()`).

Run `firmware/neokey/test/run.sh` to test this logic (colors, ramp, pulse
timing, the uncertain override) against a plain host C++ compiler, no
board or Arduino toolchain required.

## Command Keys

The first command map should be small and deliberate. The intended console has six external command keys in addition to the four external agent keys.

| Control | Agents Layer | Commands Layer | System Layer |
| --- | --- | --- | --- |
| Agent Key 1-4 | Select/focus agent slot | Select/focus agent slot | Select/focus agent slot |
| Command Key 1 | Approve plan, hold to confirm | Approve plan, hold to confirm | Brightness down |
| Command Key 2 | Request code review | Request code review | Brightness up |
| Command Key 3 | Apply reasoning effort | Continue/run selected command, hold to confirm | Select/confirm |
| Command Key 4 | Hold-to-talk microphone | Hold-to-talk microphone | Hold-to-talk microphone |
| Command Key 5 | Custom slash command | Custom slash command, hold to confirm if mutating | Diagnostics/status |
| Command Key 6 | Back/cancel/stop | Back/cancel/stop | Back/cancel |
| Rotary | Reasoning effort | Command selection | Setting value |

Bring-up mapping while only onboard KEY1-KEY4 are connected:

| Onboard Control | Temporary Meaning |
| --- | --- |
| KEY1 | Previous/select earlier agent or command |
| KEY2 | Next/select later agent or command |
| KEY3 | Apply/confirm |
| KEY4 | Hold-to-talk microphone or back/cancel depending on screen |

Decision: approval remains an explicit button press. The device should never auto-approve a plan because a selector happens to land on approval.

Decision from Claude review: any mutating action that can approve, run, or send a slash command gets a deliberate gesture. Use a hold or a confirm screen before the host bridge sends the final command.

## Custom Slash Command

KEY4 on the Commands layer should trigger a configurable slash command.

Initial placeholder:

```text
/review
```

Better future behavior:

- A small config file on the host maps the button to a slash command.
- The screen shows the current mapped command before sending it.
- Holding KEY4 opens a chooser for recent or favorite slash commands.

## Host Bridge

The ESP32-S3 firmware should stay focused on UI, inputs, status display, and audio capture.

Decision: a host-side bridge on the Mac should translate device events into Codex actions.

Suggested event names:

```text
agent.select
agent.focus
agent.reasoning.preview
agent.reasoning.apply
voice.toggle
voice.hold.start
voice.hold.stop
plan.approve
review.request
slash.run
system.status
```

Likely transport options:

| Transport | Use | Notes |
| --- | --- | --- |
| USB serial | First implementation | Simple, already works |
| USB HID | Later polish | Feels like a real control device |
| Wi-Fi/WebSocket | Later optional | Useful if device is not tethered |

Decision: start with USB serial JSON-lines events. It is the fastest bridge from working firmware to real Codex commands.

Decision: the bridge should launch and own the agent slots rather than trying to discover unrelated terminals after the fact. This matches the physical console model: Agent Key 1 maps to bridge-launched slot 1, Agent Key 2 maps to bridge-launched slot 2, and so on.

Decision: nothing useful happens on the Mac unless the bridge is running. The ESP32 board is the physical controller; the Mac bridge is the driver/daemon that listens for button events and turns them into local agent actions.

Decision: plugging in or resetting the board should not auto-launch an agent by itself. The firmware should emit agent-select events from user input, and the bridge should launch/register an empty slot only when that slot's button event arrives.

Bridge status: the first Mac-side bridge exists and is observe-only for mutating actions. It can launch/register Codex or Claude CLI sessions into stable slots, track slot status/effort/activity in its registry, open the USB serial port, read firmware JSON events, print human-readable output, and send `agent.update` status lines back to the board. It resolves Codex from the ChatGPT app bundle on Matthew's Mac when `codex` is not on PATH. It deliberately does not approve plans, run commands, focus tasks, send text into agents, or record audio yet.

Example event:

```json
{"event":"agent.reasoning.apply","agent":"current","effort":"high"}
```

Agent selection event:

```json
{"event":"agent.select","slot":3}
```

Agent focus event:

```json
{"event":"agent.focus","slot":3}
```

Slot contract decision: slots should be stable until explicitly reassigned. A completed or idle agent can stay pinned in its slot so `agent.select` with a slot number remains meaningful and agents do not silently reorder under the user's fingers.

## Screen Layout Direction

The next screen should move from static layer text to a compact agent console.

Proposed Agents layer:

```text
CODEX MICRO CONSOLE

AGENT  3/4        HIGH

review-api-fix
WORKING
main/APP/lv_mainstart.c

KEY 3: ORANGE PULSE
ROTARY EFFORT
C3 APPLY  C4 HOLD MIC
```

Proposed Commands layer:

```text
COMMANDS

Approve Plan
Request Review
Run /review

C1 HOLD APPROVE
C2 REVIEW
C3 HOLD RUN
C4 HOLD MIC
C5 /SLASH
C6 BACK
```

## Near-Term Firmware Plan

1. Done: switch the firmware config to the board's detected 16 MB flash layout, then rebuild and reflash.
2. Done: add an in-memory model for four fake agent slots with name, family, status, activity, effort, and stable slot assignment.
3. Done: replace the placeholder display with a first Agents screen while still using onboard KEY1-KEY4 as temporary bring-up controls.
4. Done: render a selected-agent card with text status, status color, effort label, and slot number.
5. Partly done: emit USB serial JSON-lines events for simulated agent selection, reasoning changes, and microphone hold/release. Command-key events are waiting for external command inputs.
6. Done: build a tiny Mac-side bridge that listens to the board and prints events.
7. Done: add bridge-owned agent slot launching, registration, and status tracking for Codex/Claude CLI sessions.
8. Done: add bridge-to-board `agent.update` messages so registry status can update the screen.
9. In progress: bench-test one external switch input. Current firmware maps GPIO44 (RXD0) to External Agent Key 1, wired as `GPIO44 ---- switch ---- GND` with internal pull-up enabled in software.
10. Bench-test one per-key RGB candidate for brightness, diffusion, color separation, and input wiring.
11. Choose the external input architecture: direct spare GPIO pins, keypad matrix, or external I2C GPIO expander/key scanner.
12. Scale the firmware input model to the intended ten controls: four agent keys plus six command keys.
13. Add illuminated-key or WS2812 status output after the hardware candidate and status model are stable.
14. Map one real command at a time into Codex after the event stream is stable.
15. Add a polished install/startup path for new users, likely a signed/helper app later. The current prototype path is a user-level macOS LaunchAgent installed by `host/install_bridge_launch_agent.py`.
16. Add microphone capture only after the audio path is verified with a minimal test.

## Hardware Direction Change: Bare Devkit + Detached Display

The board this project started on (Hiwonder WonderLLM, see "Verified Hardware Facts") is more than Switchboard needs — camera, audio codec, IMU, EEPROM, and servo bus all go unused. Decision: the next physical build moves to a bare **ESP32-S3-DevKitC-1** plus a **separate, cable-connected 2.0" ST7789 display** (non-touch — touch was never used), so the screen can be mounted independently in a 3D-printed cradle instead of being fixed to the controller board's footprint. None of the NeoKey/rotary/I2C work depends on the old board's specific hardware, so this is a clean swap.

## Proof-of-Concept Hardware — Locked

For the first physical build (not the eventual custom-PCB version below), the parts list is locked as:

| Part | Role | Status |
| --- | --- | --- |
| ESP32-S3-DevKitC-1 (N16R8 preferred, matches current flash/PSRAM config) | Controller | To buy |
| 2.0" ST7789 SPI display, 320×240, non-touch | Detached screen, own cradle | To buy |
| 3× Adafruit NeoKey 1x4 QT | 12-key input + per-key NeoPixel, chained via STEMMA QT (I2C) | Ordered (Electromaker) |
| STEMMA QT / Qwiic cables (multi-pack, mixed lengths) | Chains the 3 NeoKey boards + controller | To buy |
| Gateron mechanical switches (MX-stem) | Populate the 12 NeoKey hot-swap sockets | Already have (36 on hand) |
| Blank/translucent MX-compatible keycaps (12+) | Let NeoPixel light show through | To buy |
| KY-040-style mechanical rotary encoder (CLK/DT/SW) | Reasoning-effort dial | Already have |
| 830-point solderless breadboard | Prototyping | Already have |
| 20cm assorted M-M/M-F/F-F jumper wires | Prototyping | Already have |

Physical layout locked per the design concept below: 4 columns × 3 rows (three NeoKey boards stacked as rows, not laid end to end) — left two columns are the 6 agent keys, right two are the 6 command keys, rotary dial mounted beside the screen. See the design concept for the visual: `docs/design/panel-concept/` in this repo (source `.dc.html` files), published at [Switchboard Panel Concept](https://claude.ai/code/artifact/14b2eb7b-97dd-4a3a-a644-4ff540dc7197) (three views: logical key layout, real-world scale reference, and the tilted "finished product" concept with the rotary dial and screen integrated into one shell).

Default 6-command mapping (customizable — see below):

| Key | Action |
| --- | --- |
| C1 | Approve / Continue (hold to confirm) |
| C2 | Code Review (codex has a real `codex review` subcommand; Claude Code's equivalent unverified) |
| C3 | LGTM (default: types "LGTM" as a canned message; customizable to something like `gh pr review --approve`) |
| C4 | Hold-to-talk (mic) |
| C5 | Custom slash command (user-defined) |
| C6 | Back / Cancel |

Customization mechanism: follow the same pattern as `host/agents.json` — a personal `host/commands.json` (not checked in) with a checked-in `commands.example.json` template, each key mapping to an event plus optional canned text. Not yet implemented; the Commands layer doesn't exist in firmware yet, only Agents does.

## V2 Direction: Custom Single PCB

**Status update: real design work has started.** Three KiCad 10 projects now live in
`hardware/kicad/` (`4key/`, `8key/`, `12key/`) — parametrically generated from one shared
part/net table, with clean ERC and clean DRC, power/ground/I2C and the full per-key LED chain
routed, and the key-matrix and USB pair still left for manual routing. See
`hardware/kicad/README.md` for the per-board status and the list of things that must be verified
before a fab run. The version-identification easter-egg scheme ("The Tally") lives on the
3D-printed case - badge geometry and the scheme description are in `hardware/enclosure/`.

Once the POC validates the concept, the better long-term hardware is a single custom PCB rather than three purchased NeoKey modules — this trades "buy it, plug it in" convenience for a cheaper, more cohesive finished product. Not started; documented here so the tradeoff is explicit before committing.

**Architecture:** one PCB shaped to the actual tilted-numpad enclosure, carrying:
- A cheap I2C GPIO expander (XL9555 — already have working firmware for one — or a PCF8575) reading all 12 switches on one I2C bus, instead of three separate Seesaw-based NeoKey boards each with their own microcontroller.
- One WS2812/SK6812 addressable LED chain for all 12 per-key RGB indicators, instead of NeoKey's per-board NeoPixel driver.
- Optionally the rotary encoder footprint and even the display connector on the same board, consolidating what are currently 3-4 separate physical modules into one.

**Aesthetics/design freedom gained:**
- Exact keyswitch spacing and panel shape can match the enclosure precisely, instead of being constrained by three pre-made modules' mounting-hole patterns.
- Mounting bosses, connector placement, and silkscreen labeling are fully custom.
- Black PCB with white silkscreen (a near-zero-cost option at most fab houses) reads more "instrument panel" than a stack of visible purchased breakout boards.
- A single LED data chain can be routed for a deliberate per-key diffusion/underglow pattern rather than whatever each NeoKey board happens to do.

**Cost comparison** (button-bank subsystem only — screen, devkit, switches, keycaps, and encoder cost the same either way, so they're excluded from this comparison):

| | POC (3× NeoKey 1x4 QT) | Custom PCB |
| --- | --- | --- |
| Parts, per unit | ~$18-24 (3 boards) | ~$5-10 (expander chip ~$1, 12× WS2812 ~$2-4, bare PCB ~$1-3/unit at 5pc minimum order, passives/connectors ~$1-2) |
| Assembly | None — plug and play | Hand-soldering an SMD IC and 12 LEDs yourself, or a fab assembly service (~$15-40+ setup, better value at higher quantities) |
| Design work | None | PCB layout in KiCad/EasyEDA, a real time investment, plus a full re-fab cycle (cost + weeks of lead time) if a footprint or pinout mistake ships |
| Minimum order | Buy exactly what you need | Most fabs require ordering 5+ boards even for "one" — first attempt likely costs *more* than the POC unless you want spares or plan multiple units |

Bottom line: the custom PCB is meaningfully cheaper and more polished per unit, but only pays off once the design is proven and/or you're building more than one — which is exactly why the POC is being built with off-the-shelf NeoKey boards first rather than skipping straight to custom hardware.

## Open Questions

- What exact slash command should KEY4 run first?
- Should KEY4 on the Agents layer be tap-to-toggle mic, hold-to-talk, or both?
- Should reasoning effort changes apply instantly or require confirmation?
- How should an empty stable slot attach to a new CLI agent: manual assignment, automatic fill, or host-side prompt?
- Which illuminated key hardware should we use for per-key RGB status?
- Should the first external input test use direct GPIO, a keypad matrix, or an I2C GPIO expander?
- Can the chosen key hardware distinguish orange/amber and red solid/yellow fast flash reliably across a desk?
- Should done be steady green, steady blue, or green for successful completion and blue for available output to review?
- Which exact 16 MB partition layout should we use before adding audio and host-bridge features?
- Is four agent slots enough for the first real console, with extra active agents handled by the Mac UI instead of more agent buttons?
- Is the ESP-IDF console output channel currently UART0 or USB-Serial-JTAG? Must confirm it does not share a byte stream with the `usb_serial_jtag` host-bridge protocol before that protocol carries more traffic.
- Is the rotary encoder on hand a bare mechanical encoder (raw GPIO + interrupts) or an I2C breakout (plugs into the free `IIC1` bus on GPIO4/GPIO5)? This decides whether it needs new GPIO wiring at all.
- Which physical pins on the board's general-purpose GPIO header (silkscreened near "IO43") are actually broken out and free, confirmed by continuity/multimeter against the schematic's net list — needed before wiring any encoder pins beyond GPIO44.
