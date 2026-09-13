# Switchboard Panel Concept — Design Source

Source files for the published design canvas:
https://claude.ai/code/artifact/14b2eb7b-97dd-4a3a-a644-4ff540dc7197

Two pages, six artboards:

**Enclosure Concept:**
- `Main.dc.html` — logical key layout (6 agent keys + 6 command keys, board-grouping annotated)
- `Scale.dc.html` — real-world scale/proportion reference (4x3 grid at MX keycap pitch, screen module at relative size)
- `Hero.dc.html` — tilted, integrated "finished product" concept: screen + rotary effort dial up top, key deck tilted like a numpad, one printed shell

**Custom PCB Directions** (for the V2 custom-PCB build — see the design doc):
- `PcbBare.dc.html` — no shell, exposed matte-black PCB with gold edge castellations, keys direct on the board
- `PcbAcrylic.dc.html` — frosted acrylic sandwich case, brass standoffs, diffused RGB glow
- `PcbConsole.dc.html` — angular shell with a windowed viewport onto the PCB/LEDs, vent slots

`canvas.json` lays all six out on one pan/zoom canvas across the two pages. These are Claude Design Components (`.dc.html`), not standalone web pages — see the published link for the interactive version.

See "Proof-of-Concept Hardware — Locked" and "V2 Direction: Custom Single PCB" in `../../CODEX_MICRO_CONSOLE_DESIGN.md` for the parts list and design rationale these mockups are based on.
