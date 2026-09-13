# Switchboard: first agent console

This is a separate copy of Hiwonder `Source Code/06_lvgl_font`. The original source and working Arduino sketch have not been modified. This firmware has been built and flashed successfully on the board as the first Switchboard agent-console prototype.

## What this version does

A dark 320 x 240 screen shows CODEX MICRO CONSOLE, a selected agent card, status, activity, current reasoning effort, a four-agent status bank, and the first command-bank labels.

- Four neutral agent slots are modeled in firmware: `Agent 1`, `Agent 2`, `Agent 3`, and `Agent 4`. These are slot identities, not pretend task labels. All slots boot as empty until the bridge owns them.
- Six command slots are modeled in firmware: approve, review, run, microphone, slash command, and back/cancel.
- KEY1: select previous agent. User-initiated selection emits an `agent.select` slot event.
- KEY2: select next agent. User-initiated selection emits an `agent.select` slot event.
- KEY3: cycle/apply reasoning effort for the selected agent.
- KEY4: hold-to-talk microphone event; press emits start and release emits stop.
- Actions are demonstrations only: no Mac command, test execution, approval, microphone recording, or host connection is implemented yet.
- The board emits JSON-lines style events over USB serial for user-initiated agent selection, reasoning effort, and voice hold/release. It also listens for compact `agent.update` JSON lines from the bridge so the Mac registry can update slot name/status/effort/activity on screen. Host-to-board updates now use the USB Serial/JTAG driver directly and reply with `agent.update.ack`, so the bridge can confirm the screen accepted the update. The board does not emit agent selection on boot, so plugging in the board does not auto-launch an agent.
- Key scanning uses XL9555 input bits P04–P07, active low, with 40 ms debounce. Reads occur in the LVGL task. Holding a button does not repeat.
- There is no LED layer feedback in this display version. The supplied LED component uses GPIO1/GPIO2, while the previously working Arduino sketch used GPIO43/GPIO44. GPIO2 is also LCD chip select in this sample, so vendor LED initialization and the LED task are disabled.

## Source structure and initialization

| File | Role |
| --- | --- |
| `main/main.c` | NVS, I²C1, XL9555, then LVGL startup |
| `main/APP/lvgl_demo.c` | LVGL initialization, display buffers, touch registration, 1 ms tick, 10 ms UI task loop |
| `main/APP/lv_mainstart.c` | New agent-console screen, fake state model, key behavior, and USB event output |
| `components/BSP/RGBLCD/ltdc.c` | Despite its directory name, initializes an SPI ST7789 LCD |
| `components/BSP/TOUCH/` | Vendor touch driver and coordinate mapping |
| `components/BSP/XL9555/` | Expander initialization, keys, backlight and other board outputs |
| `components/LVGL/` | Bundled LVGL 8.3.10 source |
| `sdkconfig`, `dependencies.lock` | ESP32-S3 configuration; supplied ESP-IDF version 5.4.3 |

Display startup calls `lv_init()` then `lv_port_disp_init()` → `lcd_init()`. The LCD uses SPI2, MOSI47, SCLK21, CS2, DC3, no hardware reset, 60 MHz, RGB565. Landscape settings are 320 × 240, swapped XY, mirrored X, inverted color. Backlight remains on XL9555 mask 0x4000. Two DMA buffers hold 60 rows each (38,400 bytes per buffer at RGB565).

XL9555 remains address 0x20 on SDA38/SCL48 with vendor configuration 0x03F2. Touch retains the vendor initialization and coordinate transform. No wiring changes are needed for the four onboard buttons.

## Changes beyond the screen

1. Disabled vendor LED initialization/task to avoid GPIO2 contention.
2. Registered SPI color-transfer completion to call `lv_disp_flush_ready()` only after DMA finishes. Previously the flush callback released buffers immediately after queueing a transfer.
3. Added display-buffer allocation checks.
4. Removed the startup `lcd_clear()` call, which freed its buffer immediately after queueing DMA. LVGL paints the initial screen. Other vendor low-level drawing helpers remain unmodified and are not used by this UI; do not mix them with LVGL without reviewing their buffer lifetimes.
5. Enabled bundled Montserrat 32 for the large screen text; retained Montserrat 14 elsewhere.
6. Renamed the CMake project to `switchboard`.
7. Switched the firmware flash setting to 16 MB with the ESP-IDF large single-app partition table.

LCD pin assignments, SPI settings, orientation, expander outputs, and touch mapping are preserved.

## Build and hardware check

This is an ESP-IDF project, not an Arduino `.ino` sketch. Use an ESP-IDF 5.4.3 environment and open this folder as the project root. In a terminal, run `idf.py build` from this directory. On this Mac, `idf.py` is now wrapped in `.zshrc` to load `/Users/matthewknudsen/esp-idf/export.sh` automatically.

Before uploading future changes, retain the exact working Arduino sketch and its board/upload settings so you can restore the earlier firmware. The board reports 16 MB flash, and this project now declares 16 MB flash. Default serial console is UART0 at 115200; USB Serial/JTAG output also works on the tested board.

After a successful build and deliberate upload, verify:

1. CODEX MICRO CONSOLE appears with an agent card, status, activity, effort, and four agent status rows.
2. KEY1 and KEY2 move between the four fake agent slots.
3. KEY3 cycles the visible effort label and emits an `agent.reasoning.apply` event.
4. Holding KEY4 shows microphone feedback and emits `voice.hold.start`; releasing KEY4 emits `voice.hold.stop`.
5. Verify colors and orientation on the actual screen. Touch is initialized but this first UI has no touch controls.

Local checks use the bundled LVGL library with a simulated key reader to exercise UI state transitions. The ESP-IDF build and physical board flash have also been verified.

## External switch spike

The current firmware enables one external switch input for the first hardware test:

```text
GPIO15 ---- switch ---- GND
```

GPIO15 acts as External Agent Key 1. This is separate from the built-in KEY1-KEY4 buttons. The firmware uses the ESP32-S3 internal pull-up, so the switch should pull GPIO15 to ground when pressed.

See `host/EXTERNAL_KEY_SPIKE.md`.

## Design notes

The next control-surface decisions are tracked in `CODEX_MICRO_CONSOLE_DESIGN.md`.

## Host bridge

The first Mac-side bridge is in `host/switchboard_bridge.py`.

It launches/registers Codex or Claude CLI sessions into stable agent slots, then reads JSON-lines events from the board and prints a human-readable interpretation. The bridge must be running on the Mac; the ESP32 is the controller, and the bridge is the driver/listener. It is intentionally observe-only for mutating actions: approving plans, running slash commands, focusing Codex tasks, sending text into agents, and recording microphone audio are not connected yet.

Run against the board:

```sh
python3 host/switchboard_bridge.py --port /dev/cu.usbmodem2301
```

Run with sample events:

```sh
python3 host/switchboard_bridge.py --sample
```

Launch one agent slot:

```sh
python3 host/switchboard_bridge.py launch --slot 1 --name Maestro --command codex
```

Launch the four-slot example:

```sh
python3 host/switchboard_bridge.py launch-all
```
