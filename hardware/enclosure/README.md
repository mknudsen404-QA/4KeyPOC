# Enclosure version badge — "The Tally"

The version-identification easter-egg scheme for the keypad line. It lives on
the **3D-printed case**; the same marks also appear on the PCB silkscreen as a
quieter second layer (see `hardware/kicad/README.md`).

There is no full enclosure model in this repo yet — the case design is still
open (see the build-to-order/"watch it being printed" thread in
`docs/design/4key-product-line-idea.md`). What's here is the badge itself,
built so it can be dropped into whatever case you end up designing.

```
4key_badge.scad   8key_badge.scad   12key_badge.scad    parametric, with text
4key_badge.stl    8key_badge.stl    12key_badge.stl     printable preview tile
tools/gen_enclosure_badge.py                            the generator
```

## The scheme

Three marks, meant for the underside or the rear wall of the case — somewhere
an owner finds by handling the thing, not by looking at it head-on.

1. **Morse strip.** The tier's nickname in Morse: `STICK` / `SWITCH` /
   `CARROT`. Reads as decorative ribbing until somebody notices the rhythm is
   irregular.
2. **Revision tally.** *N* strokes = revision *N*, with every fifth stroke
   crossing the previous four like a real tally. Raised, at 1.6 mm pitch and
   0.8 mm relief, so it can be **counted by thumb** — you can tell two
   revisions apart in the dark without reading a character. This is the mark
   that works better in plastic than it ever could on a PCB.
3. **The mascot.** The carrot-and-stick scene, advancing one stage per tier:

   | Tier | Nickname | Mascot |
   | --- | --- | --- |
   | 4key | THE STICK | just the stick |
   | 8key | THE SWITCH | the stick, with the string hanging off it |
   | 12key | THE CARROT | stick, string, and the carrot finally on the end |

   Buy your way up the range and the reward gets closer. The three cases only
   tell the whole joke when they're next to each other.

Plus a small graffiti-style tag (`~ 4key  r1  2026-09 ~`) — the only place the
revision is written in characters.

## Using it in a case

```scad
use <4key_badge.scad>

union()      { my_case(); translate([x, y, z]) badge(mode = "emboss");  }
difference() { my_case(); translate([x, y, z]) badge(mode = "engrave"); }
```

The badge is drawn in the XY plane growing in +Z, origin at its lower-left
corner, 70 × 36 mm footprint. `emboss` gives raised marks clipped to the tile
outline; `engrave` gives marks to subtract, sunk `relief` (0.8 mm) into the
surface. Set `text_marks = false` if the fine text doesn't survive your layer
height.

**Which way round?** Emboss if it goes on a vertical rear wall (raised marks
print cleanly there and read well by touch). Engrave if it goes on the
underside, or put an embossed badge inside a shallow recess so nothing
protrudes past the feet and makes the keypad rock.

The `.stl` files are the embossed badge as a standalone tile — print one on its
own first to check the Morse dots and the tally ribs actually resolve at your
layer height before you commit to a whole case.

## Updating it for the next revision

The generator reads `hardware/kicad/tools/boarddef.py`, so the case and the
board can't disagree about what revision they are.

1. Bump `REV` (and `BUILD_TAG`) in `hardware/kicad/tools/boarddef.py`.
2. `python3 hardware/enclosure/tools/gen_enclosure_badge.py`
3. Regenerate the boards too, so the PCB's tally matches.

The tally gains a stroke and the tag restamps itself; nothing else to touch.
For a new tier, add a row to `TIERS` and add its stage to `mascot_polys()` —
keep continuing the same scene rather than drawing a new mascot, or the scheme
stops reading as one family.

## Status

Generated and geometrically self-checked: every mesh is closed (0 unmatched
directed edges) and all marks sit inside the tile with ≥4 mm margin.

**Not verified by rendering or printing** — there is no OpenSCAD on this
machine, so the `.scad` files have never been opened by OpenSCAD, and nothing
has been sliced. Before relying on them: open one in OpenSCAD, confirm it
compiles and that `text()` finds the font, and test-print a tile.

The `.stl` files are a union of overlapping closed volumes (tile + one prism
per mark) rather than a single manifold shell. Every slicer handles that
correctly, but a strict mesh validator will call it non-manifold; that's
expected, not a defect.
