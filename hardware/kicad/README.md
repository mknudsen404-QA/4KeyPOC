# Switchboard keypad PCBs (KiCad 10)

Three real KiCad projects for the tiered USB agent-status keypad line described in
`docs/design/4key-product-line-idea.md`, and the V2 custom-PCB direction in
`SWITCHBOARD_DESIGN.md`.

| Project | Keys | Grid | Nickname on silkscreen | Board size |
| --- | --- | --- | --- | --- |
| `4key/` | 4 (3 agent slots + PTT) | 1 × 4 | THE STICK | 87.2 × 70.5 mm |
| `8key/` | 8 (7 agent slots + PTT) | 2 × 4 | THE SWITCH | 87.2 × 89.6 mm |
| `12key/` | 12 (11 agent slots + PTT) | 3 × 4 | THE CARROT | 87.2 × 108.7 mm |

The `4key` board is the drop-in electrical equivalent of the currently shipping
`firmware/neokey/switchboard_neokey.ino` build: same 4 keys, same 3-agent + 1
push-to-talk split, same USB-serial JSON-lines protocol to
`host/switchboard_bridge.py`. It replaces the purchased Adafruit NeoKey 1x4
(and its Seesaw microcontroller) with one XL9555-class I2C expander and one
addressable-LED chain, per the V2 direction.

**Nothing here has been fabricated.** Read "Before you send these to a fab"
at the bottom — several dimensions in here are derived from community
references rather than manufacturer datasheets and must be verified.

## These files are generated

Do not hand-edit the `.kicad_sch` / `.kicad_pcb` files and expect the edit to
survive. One parametric definition drives all three tiers:

```
tools/boarddef.py   the single source of truth: parts, pin->net map,
                    placements, tier table, easter-egg data
tools/gen_sch.py    -> <tier>/<tier>.kicad_sch, .kicad_pro, sym-lib-table,
                       fp-lib-table   (system python3)
tools/gen_pcb.py    -> <tier>/<tier>.kicad_pcb   (KiCad's bundled python +
                       the pcbnew API, which is why it needs the interpreter
                       inside KiCad.app)
lib/switchboard.pretty/SW_Hotswap_Kailh_MX.kicad_mod
                    the one custom footprint (see caveats)
```

Regenerate everything:

```sh
cd hardware/kicad/tools
python3 gen_sch.py
/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3.9 gen_pcb.py
```

Generating both sides from one table is deliberate: it is what guarantees the
schematic netlist and the board's pad nets cannot drift apart. If you start
editing the boards by hand in KiCad (which you will, to finish the routing),
stop regenerating and treat the files as the source from then on.

## Shared architecture (identical on all three tiers)

| Ref | Part | Role |
| --- | --- | --- |
| U1 | ESP32-S3-WROOM-1 (N16R8) | Controller. Native USB device, I2C master, LED data out. |
| U2 | XL9555 / PCF8575, TSSOP-24 | 16-bit I2C GPIO expander; reads every key. A2:A1:A0 = 000 → **0x20**. |
| U3 | AP2112K-3.3, SOT-23-5 | 5 V VBUS → 3V3 for U1 and U2. |
| U4 | 74LVC1G17, SOT-23-5 | Schmitt buffer powered from 5 V, so the LED chain sees a legal VIH instead of the ESP32's 3.3 V. |
| J1 | USB-C receptacle, 16P | Power + native USB serial. Sink only, 5.1 k CC pulldowns (R2/R3). |
| J2 | 2×6 pin header | Spare I2C + GPIO + rails, for a future encoder/display/second expander. |
| SW1..n | Gateron/MX-stem switch in a Kailh hot-swap socket | Keys. No switch is soldered to the board. |
| D1..n | SK6812MINI (PLCC4 3.5 × 3.5 mm) | One per-key RGB, top-mounted in the switch's SMD-LED window, daisy-chained. |
| CL1..n | 100 nF 0603 | One decoupling cap per LED. |

Firmware-visible pin map (matches `firmware/neokey/`'s `SDA_PIN 9` / `SCL_PIN 8`):

| ESP32-S3 pin | Net |
| --- | --- |
| IO9 | SDA |
| IO8 | SCL |
| IO5 | LED data (into U4, then a 330 Ω series resistor R7, then D1) |
| IO4 | expander `/INT` (so key scanning can be interrupt-driven instead of polled) |
| IO0 | BOOT button |
| EN | RESET button + RC |
| USB_D+ / USB_D− | J1 |

Key mapping rule for the whole line: **the bottom-right key is always
push-to-talk; every other key is an agent slot.** That keeps the firmware
generalisation trivial (`AGENT_KEY_COUNT = n-1`, `PTT_KEY_INDEX = n-1`) and
means the mic key doesn't move when a customer upgrades tiers. The trade-off
at the top tier is 11 agent slots, which is more parallel agents than most
people run; if you'd rather spend two of those keys on Approve/Cancel, that is
a `boarddef.py` edit plus a firmware change, not a board respin.

### What differs per tier

Only the key count, the row count (1/2/3 rows of 4), the board height, and the
easter-egg values. Same schematic, same parts, same expander: a single XL9555
has 16 I/O, so even the 12-key tier uses one chip with 4 spare I/O (brought
out as no-connects on the schematic). A second chained expander at 0x21 would
be needed only past 16 keys.

### Deliberate design notes

- **2-layer board, ground pour on both sides.** Signals run on F.Cu; the three
  nets that would otherwise have to cross everything (3V3 into the left-hand
  region, SDA, SCL) dive to B.Cu and cross underneath the pour, which flows
  around them. A handful of stitching vias tie the two pours together.
- **The ESP32-S3 PCB-antenna keepout is honoured**, as a notch in both ground
  pours plus the module footprint's own keepout. That is why the four mounting
  holes are not a symmetric rectangle — the top-left corner is inside the
  antenna keepout. This product does not use Wi-Fi/BLE, but the keepout is
  cheap to respect and leaves the option open.
- **Hot-swap only.** No switch solders to the board; MX-stem switches drop into
  Kailh sockets, matching the locked POC parts list.

## Easter-egg scheme: "The Tally" — lives on the enclosure

The version-identification scheme belongs to the **3D-printed case**, not the
board: see **`hardware/enclosure/`** for the badge geometry (parametric
`.scad` plus printable `.stl` per tier) and the full description of the
scheme. Short version — a Morse strip spelling the tier nickname, a revision
tally you can count by thumb, and a carrot-and-stick mascot that advances one
stage per tier.

The same marks are *also* on the back of each PCB, as a quieter second layer
only somebody who opens the case will ever see:

- **Morse strip** and **revision tally** on B.Silkscreen, bottom-left.
- **Graffiti tag** (`~ stick  r1  2026-09 ~`) instead of a sterile rev block.
- **The mascot** as a solder-mask *opening* over the back ground pour, so it
  comes out as bare plated copper on matte black. Being a mask aperture rather
  than a floating copper island, it costs nothing at the fab and can't cause a
  clearance problem.

Both sets are driven from `REV` / `BUILD_TAG` / `TIERS` in
`tools/boarddef.py`, so the case and the board can't disagree about which
revision they are — bump `REV`, regenerate both, done. If you'd rather the
board stayed plain and the case carried the whole joke, delete the
`easter_eggs()` call in `gen_pcb.py`'s `run()` and regenerate; nothing else
depends on it.

## ERC / DRC status — honestly

Checked with KiCad 10.0.6 (`kicad-cli`), all severities enabled.

| Board | ERC | DRC violations | DRC unconnected items |
| --- | --- | --- | --- |
| 4key | **0** | **0** | 34 |
| 8key | **0** | **0** | 38 |
| 12key | **0** | **0** | 42 |

ERC is genuinely clean: every pin is either on a net or carries an explicit
no-connect, both rails have power flags, and the LDO output drives 3V3.

DRC reports **zero rule violations** — no clearance, crossing, shorting,
mask-bridge, courtyard, hole or silkscreen errors. The remaining
"unconnected items" are nets this generator deliberately did **not** route:

**Routed (by the generator):**

- GND — pours on F.Cu and B.Cu, thermal-relieved, with stitching vias.
- +5V — USB VBUS → bulk caps → LDO input/enable → level-shifter supply →
  right-margin trunk → a per-row bus feeding every LED's VDD and every
  per-key decoupling cap.
- +3V3 — LDO output → its bulk/decoupling caps → expander VCC → across the
  board on B.Cu → module 3V3 pin and its decoupling cap.
- SDA / SCL — module to expander, on B.Cu under the pour.
- The whole LED subsystem — IO5 → buffer → series resistor → `LEDCH0..n`
  daisy chain, including the row-to-row wraps on the 8- and 12-key boards.

**Left as ratsnest, for a human in the KiCad PCB editor:**

- `KEY1..KEYn` — every switch's signal pin to its expander I/O. Deliberate:
  this is the part where you actually want to choose the trace pattern by
  hand, and the assignment of key ↔ expander pin is still cheap to change.
  (Each switch's other pole already reaches the ground pour directly.)
- `USB_DP` / `USB_DM` — the USB 2.0 pair. Left for manual routing on purpose:
  a differential pair off a 0.5 mm-pitch connector wants a person's eye, not a
  script, and it is the one net where sloppy routing shows up as a device that
  intermittently fails to enumerate.
- `CC1` / `CC2` to R2/R3 — trivial, but in the same fine-pitch fan-out.
- `~RESET` and `BOOT` — button and RC nets in the left-hand region.
- `EXP_INT` and its pull-up R6.
- The I2C pull-ups R4/R5 and the EN pull-up R1 — placed, not wired.
- `J2` header nets (`HDR_*`).

So: power, ground, I2C and the entire LED chain are routed; the key matrix,
USB pair, and a handful of pull-ups/buttons are not. Expect an hour or two in
the PCB editor per tier to finish, and re-run DRC after.

Known non-blocking rule relaxations, set by `gen_pcb.py` and visible in the
board's design settings: minimum through-hole drill 0.15 mm (the
ESP32-S3-WROOM-1 footprint's own thermal vias are 0.2 mm), and minimum
resolved thermal spokes 1 instead of 2 (thermal relief is kept for
hand-soldering rather than switching the pour to solid connections).

## Symbols and footprints: what is real and what is a stand-in

All symbols and footprints are KiCad 10's bundled libraries except one custom
footprint. Things to be aware of:

- **U2's symbol is `Interface_Expansion:PCF8575DBR`, used as a stand-in for the
  XL9555.** The XL9555 in TSSOP-24 is understood to share the PCF8575's
  pinout, and the firmware already drives an XL9555 — but the symbol, the
  value field, and the BOM all say PCF8575DBR. **Verify the XL9555 pinout
  against its datasheet before ordering**, or just buy PCF8575s.
- **`lib/switchboard.pretty/SW_Hotswap_Kailh_MX.kicad_mod` is hand-authored.**
  Its socket pad positions and pin-hole diameters come from widely-used
  community references for the Kailh CPG151101S11, **not** from the Kailh
  datasheet. Its courtyard is also deliberately clipped short on the south side
  so the in-switch LED can sit inside it. This is the single most likely thing
  on these boards to be wrong in a way that costs a fab run.
- **The per-key LED is placed 5.0 mm south of the switch centre**, on the
  assumption that the MX SMD-LED window is opposite the contact pins. That
  offset is an assumption, not a measurement.
- The ESP32-S3-WROOM-1, USB-C, TSSOP-24, SOT-23-5, SK6812MINI, MountingHole,
  0603/0805 and pin-header footprints are all stock KiCad and should be fine,
  but the USB-C receptacle footprint is for a specific part
  (`HCTL HC-TYPE-C-16P-01A`) — match what you actually buy.

## Before you send these to a fab

In rough order of how expensive it is to get wrong:

1. **Print the key field 1:1 and test-fit a real Kailh hot-swap socket, a real
   Gateron MX switch, and a real SK6812MINI.** Confirm the socket pads, the
   3.05 mm pin holes, the 4 mm centre hole, the ±5.08 mm plate holes, and
   especially the LED's 5.0 mm offset and which side of the switch its window
   is on. This is the one check that cannot be skipped.
2. **Verify the ESP32-S3-WROOM-1 footprint against Espressif's datasheet**
   (pad pitch, thermal-pad size, keepout), and confirm the module variant you
   buy is N16R8 — the octal PSRAM on that variant consumes IO35/36/37, which
   is why those are no-connects here and must not be repurposed.
3. **Confirm 0x20 is free on the I2C bus** and that nothing else the firmware
   talks to collides. If you ever chain a second expander for >16 keys, it
   needs A0 tied high for 0x21.
4. **Finish the unrouted nets** listed above, then re-run DRC, then re-run
   `kicad-cli sch erc` after any schematic change.
5. **Decide the LED supply question for real.** The design runs the SK6812MINI
   chain at 5 V with a 5 V Schmitt buffer on the data line, which is the
   correct fix for the 3.3 V VIH problem — but check the actual SK6812MINI part
   you buy for its VDD range and current, and budget the 5 V rail for n × ~60 mA
   worst case (12 keys at full white is ~0.7 A, which a USB-C port will supply
   but the trace widths and the 22 µF bulk cap should be sized for).
6. **Check the USB-C shield-to-GND connection.** It is tied directly to GND
   here; some designs prefer a cap or ferrite. Fine either way, but decide it.
7. **Review the mounting-hole pattern against the actual 3D-printed case**,
   since the antenna keepout forced an asymmetric layout.
8. **Trademark-check the tier nicknames before they go on a product.**
   "THE SWITCH" in particular is on the silkscreen and would need a real search
   in the relevant classes — see the IP section of
   `docs/design/4key-business-plan.md`. Nothing here uses "Claude" or "Codex".
9. Order a black-soldermask / white-silkscreen board (already set in the
   stackup) and ENIG if you want the bare-copper mascot to look good and not
   oxidise.
