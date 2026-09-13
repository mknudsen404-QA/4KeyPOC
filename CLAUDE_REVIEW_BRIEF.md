# Claude Review Brief: Codex Micro Console

Please review this project as a second technical/product-design pass.

## Context

Codex Micro Console is an ESP32-S3 hardware control surface for AI coding work. It has a built-in 320 x 240 ST7789 display, FT62xx touch controller, XL9555 GPIO expander for onboard keys, USB serial, likely ES8311/I2S audio hardware, and space for future external controls.

The first firmware proof-of-life is working on-device:

- Project name: `codex_micro_console`
- Screen title: `CODEX MICRO CONSOLE`
- Display initializes and renders LVGL UI.
- Touch controller initializes.
- KEY1 through KEY4 read through XL9555.
- Firmware builds and flashes with ESP-IDF 5.4.3.

## Current Project Location

```text
/Users/matthewknudsen/Documents/Codex-Micro-Matt/Codex_Micro_Display
```

## Main Files To Review

```text
/Users/matthewknudsen/Documents/Codex-Micro-Matt/Codex_Micro_Display/CODEX_MICRO_CONSOLE_DESIGN.md
/Users/matthewknudsen/Documents/Codex-Micro-Matt/Codex_Micro_Display/README_CODEX_MICRO.md
/Users/matthewknudsen/Documents/Codex-Micro-Matt/Codex_Micro_Display/main/APP/lv_mainstart.c
/Users/matthewknudsen/Documents/Codex-Micro-Matt/Codex_Micro_Display/main/APP/lvgl_demo.c
/Users/matthewknudsen/Documents/Codex-Micro-Matt/Codex_Micro_Display/components/BSP/RGBLCD/ltdc.c
/Users/matthewknudsen/Documents/Codex-Micro-Matt/Codex_Micro_Display/components/BSP/XL9555/xl9555.c
```

## Stable References

```text
/Users/matthewknudsen/Documents/Codex-Micro-Matt/Codex_Micro_Display/docs/reference/5. LVGL Development Tutorial.pdf
/Users/matthewknudsen/Documents/Codex-Micro-Matt/Codex_Micro_Display/docs/reference/6. ESP32-S3 Development Board Diagram_V1.1.pdf
/Users/matthewknudsen/Documents/Codex-Micro-Matt/Source Code
/Users/matthewknudsen/Documents/Codex-Micro-Matt/Arduino Program
```

## Product Direction

The device should become a physical console for:

- Cycling through active agents/tasks.
- Seeing each agent's current status and activity.
- Adjusting reasoning effort with the rotary dial.
- Starting microphone input from a dedicated key.
- Sending deliberate command intents such as approve plan, request code review, continue/run, and custom slash command.
- Showing Codex vs Claude identity with small 8-bit character sprites.
- Showing reasoning effort with a tiny 8-bit thought meter.

## Requested Review

Please critique:

1. Whether the proposed layer model is simple enough for a small hardware console.
2. Whether the control mapping is ergonomic:
   - KEY1 previous agent / approve plan
   - KEY2 next agent / request review
   - KEY3 apply effort / run selected command
   - KEY4 microphone / custom slash command
   - rotary dial reasoning effort or selection
3. Whether the 8-bit Codex/Claude character idea clarifies identity or risks becoming visual noise.
4. Whether the status-color language is understandable:
   - white idle
   - purple thinking
   - blue editing
   - amber testing
   - cyan waiting
   - yellow needs input
   - red blocked
   - green done
5. Firmware risks to resolve before adding host-bridge commands.
6. Hardware risks to resolve before adding mic capture, rotary input, and LEDs.
7. What the next smallest useful firmware milestone should be.

## Known Risks

- The board reports 16 MB flash, but the current firmware image still declares 2 MB flash and uses a single-app partition.
- The display demo does not include rotary encoder handling yet.
- The microphone path is inferred from Hiwonder `11_bootloader` ES8311/I2S references and still needs verification.
- Host-side Codex control is not implemented yet.
- Current key actions are local UI feedback only.
- Vendor LED code conflicted with the display sample, so LED status is intentionally deferred.

## Preferred Output

Please return:

- Top five concerns.
- Top five product/design suggestions.
- Recommended next firmware milestone.
- Any specific code or hardware areas that should be inspected before implementation.
