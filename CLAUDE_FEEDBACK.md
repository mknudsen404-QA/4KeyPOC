# Claude Feedback Summary

Captured: 2026-09-05

Claude reviewed the Codex Micro Console direction and confirmed that the current firmware is a clean proof-of-life: minimal LVGL UI, key polling, no persistence, no rotary input, no sprites, and no host bridge yet.

## Main Concerns

1. Fix the flash/partition mismatch before adding larger features. The board reports 16 MB flash, while the current project still declares a 2 MB firmware image and single-app partition layout.
2. Treat voice as a cross-layer gesture rather than a full peer layer. A microphone action should be available while looking at an agent or command, not hidden behind an extra mode switch.
3. Audit KEY3 and KEY4 meanings across layers. Anything that can approve, run, or mutate work should require a deliberate gesture such as a hold or confirm step.
4. Do not rely on tiny 8 x 8 sprites to communicate important state. They can provide charm and identity, but status must be text-first and color-supported.
5. Add an in-firmware state model before the host bridge. The UI should render from agent/status/effort structs so local UI, serial events, and host state stay coherent.

## Product Suggestions

1. Use three primary layers for v1: Agents, Commands, and System.
2. Make microphone input available as a KEY4 hold gesture from multiple layers.
3. Keep status words on screen, with color as reinforcement.
4. Treat the rotary dial as relative detents, not absolute physical positions.
5. Print structured serial events before building the host bridge or polishing sprites/LEDs.

## Recommended Next Firmware Milestone

1. Switch the project to the board's 16 MB flash configuration.
2. Add an in-memory agent/status/effort model with two or three fake agents.
3. Render selected agent name, status, and reasoning effort on the existing screen.
4. Make KEY1 and KEY2 cycle selected agents.
5. Make KEY3 and KEY4 print JSON-lines events over serial.

## Adopted Decisions

Claude's strongest product-design point is accepted: important status should move to illuminated keys/LEDs and text labels. The 8-bit Codex/Claude characters become optional identity decoration, not the primary status system.
