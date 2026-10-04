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
`firmware/neokey/neokey.ino` build: same 4 keys, same 3-agent + 1
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

Checked with KiCad 10.0.6 (`kicad-cli`), all severities enabled. Re-verified
2026-10-03 after an independent pre-fab review (one Claude session, one Opus
session cross-checking it) found and fixed three real issues in the
generator — see "Fixed from that review" below.

| Board | ERC | DRC violations | DRC unconnected items |
| --- | --- | --- | --- |
| 4key | **0** | **0** | 39 |
| 8key | **0** | **0** | 43 |
| 12key | **0** | **0** | 47 |

(Unconnected-item counts went up from 34/38/42 because of the new ESD diode's
ratsnest — see below; nothing about the previously-routed nets changed.)

### Fixed from that review

1. **`gen_pcb.py` ignored `p.nc` entirely** (`gen_sch.py` already respected
   it). A pin listed in both `p.pins` (a plausible net name) and `p.nc`
   (explicitly unconnected — e.g. the last LED's DOUT, still called
   `LEDCH%d` even though nothing's next in the chain) got wired in the PCB
   but left as a no-connect in the schematic: real drift, the exact thing
   "one definition drives both generators" is supposed to prevent. Fixed by
   checking `p.nc` first in `place_parts()`, same as `gen_sch.py`'s
   `is_nc` check. Verified directly against the saved board with `pcbnew`
   (not just DRC, which couldn't see this): D4 pad 1 now reports no net,
   pads 2-4 unchanged.
2. **No bulk cap at U1's own +3V3 pin.** Added C9 (10 µF 0805) on the same
   net, T-tapped off the existing `(73.0,25.5)-(13.0,25.5)` B.Cu +3V3 trunk.
   Not literally at the pin — the entire left margin (U1's own body, C4,
   SW_BOOT, C7, U4, C6, SW_RST, R7 and their traces) is already packed
   solid; see the code comment on C9 in both `boarddef.py` and
   `gen_pcb.py`'s `route_strip()` for exactly where it landed instead and
   why every closer spot either crossed `LED_GPIO`'s run through that
   corner or sat on top of U1's own closely-pitched pin column.
3. **No USB ESD protection.** Added D_ESD, a USBLC6-2SC6 TVS array, shunted
   across the existing USB_DP/USB_DM/+5V/GND nets right near the connector
   (pinout confirmed against the actual symbol KiCad ships —
   `Power_Protection.kicad_sym`'s `USBLC6-2P6`, which `USBLC6-2SC6`
   extends: pins 1/6 = I/O1, pins 3/4 = I/O2, 2 = GND, 5 = VBUS). Left
   unrouted like the rest of the differential pair — this only adds the
   part and its net assignments to the schematic/BOM, not new copper.
4. A doc bug: `gen_pcb.py`'s own comment about the upper-left mounting
   hole's x-position said 49, the code uses 51.5.

Not changed from that review (deliberately, these are real judgment calls,
not generator bugs): a per-key decoupling cap (CL) that may clip the switch
housing (folds into the mandatory physical test-fit below, not something
DRC can see — the footprint's courtyard is deliberately clipped), and the
5V-side level-shifter's tighter-than-ideal voltage margin (works, documented
as a known tradeoff, a part swap is a bigger decision than this pass's
scope).

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
footprint. Status as of the 2026-10-03 pre-fab review (primary datasheets
fetched and checked pin-by-pin, not just read about):

- **U2's symbol is `Interface_Expansion:PCF8575DBR`, used as a stand-in for the
  XL9555 — now independently verified, not just assumed.** Checked against
  Xinluda's real XL9535/XL9555 datasheet (Rev 2.2, Table 2 Pin Functions,
  TSSOP24 column): pin 1=INT, 2=A1, 3=A2, 4-11=P00-07, 12=GND, 13-20=P10-17,
  21=A0, 22=SCL, 23=SDA, 24=VCC — an exact match to every pin `boarddef.py`
  assigns, including the two easiest things to get backwards (SCL/SDA not
  swapped; A0/A1/A2 all correctly grounded for address 0x20). The symbol,
  value field, and BOM still say PCF8575DBR, but the electrical assignment
  underneath it is confirmed correct.
- **`lib/switchboard.pretty/SW_Hotswap_Kailh_MX.kicad_mod` is hand-authored**
  from community references, not the Kailh datasheet. Its exact pad offsets
  (switch pins at ±5.08 mm/1.7 mm drill, hotswap barrels at (-3.81,-2.54) and
  (2.54,-5.08)/3.05 mm drill, 4 mm centre hole) match a pattern replicated
  across many open-source keyboard PCBs — corroborating, not conclusive.
  Physical test-fit (below) is still the one check that cannot be skipped.
- **The per-key LED's 5.0 mm south-of-centre offset is better justified than
  "an assumption"**: it matches the standard in-switch RGB convention (LED on
  the axis opposite the switch's own contact pins) to within 0.05 mm, and
  south-facing (the direction this design uses) is the orientation that
  avoids Cherry-profile keycap interference. Still worth confirming by eye
  during the test-fit, just not the open question it was.
- **The LED part/footprint pairing needs the exact SKU pinned down before
  ordering — this is the one real gap this review surfaced.**
  `boarddef.py` uses `LED_SMD:LED_SK6812MINI_PLCC4_3.5x3.5mm_P1.75mm` (the
  *plain* SK6812MINI, not the "-E" variant — KiCad ships both as genuinely
  different footprints, different size, different mount style). The plain
  part's own datasheet confirms its pinout matches this footprint and the
  code's comment (1=DOUT, 2=VSS, 3=DIN, 4=VDD), so the design itself isn't
  wrong — but its datasheet (checked directly) doesn't list a per-channel
  current figure the way the "-E" variant's does, so the current-budget
  numbers below should be re-confirmed against whichever exact SKU you
  actually order, not assumed to carry over from the "-E" part's spec.
- The ESP32-S3-WROOM-1 footprint — **now independently verified, all 41
  pins**, against Espressif's official datasheet v1.8 (Table 3-1), not just
  "should be fine": every pin `boarddef.py` assigns matches exactly,
  including the two it would have been easy to get backwards (pin 13=IO19=
  USB_D-, pin 14=IO20=USB_D+) and the two a bad search result once claimed
  were GPIOs instead of what they really are (pin 40=GND, pin 41=EPAD/GND,
  "connect directly to ground" per the datasheet itself). Confirm the module
  variant you buy is still N16R8 — the octal PSRAM on that variant consumes
  IO35/36/37, which is why those are no-connects here and must not be
  repurposed.
- USB-C, TSSOP-24, SOT-23-5/6, MountingHole, 0603/0805 and pin-header
  footprints are all stock KiCad and should be fine, but the USB-C receptacle
  footprint is for a specific part (`HCTL HC-TYPE-C-16P-01A`) — match what
  you actually buy.

## Before you send these to a fab

In rough order of how expensive it is to get wrong. Items marked **done**
were closed out in the 2026-10-03 review (one Claude session, one
independent Opus session cross-checking it); nothing here was taken on
faith from either — see the datasheet-level detail above and in
`docs/design/hardening-pass-plan.md`'s Workstream E.

1. **Print the key field 1:1 and test-fit a real Kailh hot-swap socket, a real
   Gateron MX switch, and a real SK6812MINI** (confirm the exact SKU first —
   see above). Confirm the socket pads, the 3.05 mm pin holes, the 4 mm
   centre hole, the ±5.08 mm plate holes, the LED window side, **and that a
   per-key decoupling cap (CL) doesn't clip the switch's own housing** — the
   footprint's courtyard is deliberately clipped there, so DRC can't catch
   this one. Still the one check that cannot be skipped.
2. **Done: ESP32-S3-WROOM-1 footprint verified** against Espressif's
   datasheet, all 41 pins. Still confirm the module variant you buy is
   N16R8.
3. **Done: 0x20 confirmed free** — this is a from-scratch board, nothing
   else shares the bus. A future >16-key chained expander needs A0 tied high
   for 0x21.
4. **Finish the unrouted nets** (`KEY1..KEYn`, `USB_DP`/`USB_DM`, `CC1`/`CC2`,
   `~RESET`/`BOOT`, `EXP_INT`, the I2C/EN pull-ups, `J2`, and now D_ESD's
   pads too — see "ERC / DRC status" above), then re-run DRC, then re-run
   `kicad-cli sch erc` after any schematic change.
5. **LED supply budget, with real numbers now**: SK6812MINI-E's datasheet
   (the closest verified reference — see the SKU caveat above) confirms
   12 mA per channel max, so ~36 mA/LED at full white + ~1 mA static. 4key's
   4 LEDs is ~150 mA worst case on the 5 V rail — comfortably inside what
   the design already budgets for (the 22 µF bulk cap and trace widths were
   already sized against the 12-key tier's ~700 mA worst case, which this is
   far under). Re-confirm once the exact SKU is locked.
6. **Done: USB ESD protection added** (D_ESD, USBLC6-2SC6) and **a second
   bulk cap added at U1's own +3V3 net** (C9) — see "Fixed from that review"
   above for exactly what and why.
7. **Check the USB-C shield-to-GND connection.** It is tied directly to GND
   here; some designs prefer a cap or ferrite. Fine either way, but decide it.
8. **Confirmed: there is no enclosure/case file in this repo to check the
   mounting-hole pattern against** (`hardware/enclosure/` holds only the
   per-tier version-badge tile — see its own README). This step is currently
   impossible to do, not just undone; revisit once a case model exists.
9. **Trademark-check the tier nicknames before they go on a product — treat
   all three as internal codenames until then.** "THE SWITCH" is the one to
   worry about: it's on the silkscreen, sits in the same product class
   (electronic devices) as Nintendo's Switch trademark, and Nintendo
   enforces that mark aggressively. "THE STICK" and "THE CARROT" brush
   weaker but real neighbors (Fire TV Stick/Memory Stick; the CARROT app
   family) in the same class. All three are bare dictionary words, so even
   a clear mark would be weak — but that cuts both ways; it's not a defense
   against getting a cease-and-desist first. This needs a real TESS search
   or an attorney, not the casual web search this review used as a gut-check
   — see the IP section of `docs/design/4key-business-plan.md`. Nothing here
   uses "Claude" or "Codex". Also worth a line item next to it: the
   hot-swap footprint's community-reference provenance — some
   keyboard-community footprint libraries are share-alike licensed, which
   could attach to these PCB files; not checked this pass.
10. Order a black-soldermask / white-silkscreen board (already set in the
    stackup) and ENIG if you want the bare-copper mascot to look good and not
    oxidise. Decide fab house and batch quantity (most fabs have a 5-board
    minimum even for "one") before submitting.
