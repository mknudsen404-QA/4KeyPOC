#!/usr/bin/env python3
"""Generate <tier>.kicad_sch + <tier>.kicad_pro from boarddef.py.

Style note: this is a "stub and label" schematic. Every pin gets a 2.54mm wire
stub with a net label (or a power symbol) on the end rather than long drawn
nets. That is a normal, readable schematic style for a board that is mostly
one bus and one chain, and it is what makes the netlist safe to generate
mechanically - a label cannot be off by a grid unit the way a hand-drawn wire
can.
"""

import math
import os
import re
import sys
import uuid as uuidmod

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import boarddef  # noqa: E402

SYMDIR = "/Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols"
OUT_ROOT = os.path.normpath(os.path.join(HERE, ".."))

POWER_NETS = {"GND": "power:GND", "+3V3": "power:+3V3", "+5V": "power:+5V"}


def uid():
    return str(uuidmod.uuid4())


# ---------------------------------------------------------------------------
# Symbol library access
# ---------------------------------------------------------------------------

_libcache = {}


def _libtext(lib):
    if lib not in _libcache:
        _libcache[lib] = open(os.path.join(SYMDIR, lib + ".kicad_sym")).read()
    return _libcache[lib]


def raw_symbol(lib, name):
    txt = _libtext(lib)
    m = re.search(r'\n\t\(symbol "' + re.escape(name) + r'"\n', txt)
    if not m:
        raise KeyError("%s:%s not found" % (lib, name))
    start = m.start() + 1
    end = txt.index("\n\t)\n", start) + 4
    return txt[start:end]


def _extends_of(body):
    m = re.search(r'\n\t\t\(extends "([^"]+)"\)', body)
    return m.group(1) if m else None


def resolved_symbol(lib_id):
    """Return (body_text, pins) with `extends` flattened into one definition."""
    lib, name = lib_id.split(":", 1)
    body = raw_symbol(lib, name)
    parent = _extends_of(body)
    chain = [body]
    while parent:
        pbody = raw_symbol(lib, parent)
        chain.append(pbody)
        parent = _extends_of(pbody)
    if len(chain) == 1:
        flat = body
    else:
        # Take the child's properties, the root ancestor's graphics/pins.
        root = chain[-1]
        child = chain[0]
        child_noext = re.sub(r'\n\t\t\(extends "[^"]+"\)', "", child)
        # Everything in root from the first sub-unit "(symbol " onward.
        m = re.search(r'\n\t\t\(symbol "', root)
        graphics = root[m.start():root.rindex("\n\t)")]
        # Rename sub-units so they carry the child's name.
        rootname = re.match(r'\t\(symbol "([^"]+)"', root).group(1)
        graphics = graphics.replace('(symbol "%s_' % rootname,
                                    '(symbol "%s_' % name)
        # Inherit any property the child (and closer ancestors) never override.
        have = set(re.findall(r'\n\t\t\(property "([^"]+)"', child_noext))
        inherited = []
        for anc in chain[1:]:
            for pm in re.finditer(r'\n\t\t\(property "([^"]+)"', anc):
                pname = pm.group(1)
                if pname in have:
                    continue
                have.add(pname)
                pstart = pm.start() + 1
                pend = anc.index("\n\t\t)\n", pstart) + 4
                inherited.append(anc[pstart:pend].rstrip("\n"))
        # Same for the top-level symbol flags, which a derived symbol inherits
        # rather than restating.
        flags = []
        for key in ("pin_numbers", "pin_names", "exclude_from_sim", "in_bom",
                    "on_board", "in_pos_files",
                    "duplicate_pin_numbers_are_jumpers"):
            if re.search(r'\n\t\t\(%s[ \n)]' % key, child_noext):
                continue
            for anc in chain[1:]:
                am = re.search(r'\n\t\t\(%s[ \n)]' % key, anc)
                if am:
                    flags.append(_block(anc, am.start() + 1))
                    break
        nameline_end = child_noext.index("\n") + 1
        head = (child_noext[:nameline_end] +
                ("\n".join(flags) + "\n" if flags else "") +
                child_noext[nameline_end:child_noext.rindex("\n\t)")])
        if inherited:
            head = head + "\n" + "\n".join(inherited)
        flat = head + graphics + "\n\t)\n"
    # Rename the top-level symbol to the fully qualified lib id.
    flat = re.sub(r'^\t\(symbol "[^"]+"', '\t(symbol "%s"' % lib_id, flat, count=1)
    pins = _parse_pins(flat)
    return flat, pins


def _block(text, start):
    """Return the balanced s-expression beginning at index `start`."""
    depth = 0
    i = start
    while i < len(text):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
        i += 1
    raise ValueError("unbalanced s-expression")


def _parse_pins(body):
    """{number: (name, etype, x, y, angle)} for unit-1 pins."""
    pins = {}
    for m in re.finditer(
            r'\(pin (\w+) \w+\s*\n\s*\(at ([-\d.]+) ([-\d.]+) ([-\d.]+)\)'
            r'(?:.*?\n)*?\s*\(name "([^"]*)"'
            r'(?:.*?\n)*?\s*\(number "([^"]*)"', body):
        etype, x, y, ang, nm, num = m.groups()
        if num not in pins:
            pins[num] = (nm, etype, float(x), float(y), float(ang))
    return pins


# ---------------------------------------------------------------------------
# Emitters
# ---------------------------------------------------------------------------

def outward(angle):
    """Direction a pin's wire leaves the symbol body, in schematic coords."""
    a = math.radians(angle)
    return (round(-math.cos(a), 6), round(math.sin(a), 6))


def label_angle(dx, dy):
    if dx < 0:
        return 180
    if dx > 0:
        return 0
    return 90 if dy < 0 else 270


PWR_ROT_GND = {(0, 1): 0, (0, -1): 180, (-1, 0): 270, (1, 0): 90}
PWR_ROT_RAIL = {(0, -1): 0, (0, 1): 180, (-1, 0): 90, (1, 0): 270}


def snap(v):
    return round(v / 1.27) * 1.27


def fmt(v):
    return ("%.4f" % v).rstrip("0").rstrip(".") if isinstance(v, float) else str(v)


def emit_symbol(lib_id, ref, value, footprint, x, y, rot, pin_numbers,
                root_uuid, project, hide_value=False):
    props = [("Reference", ref, False), ("Value", value, hide_value),
             ("Footprint", footprint or "", True), ("Datasheet", "", True),
             ("Description", "", True)]
    out = ['\t(symbol',
           '\t\t(lib_id "%s")' % lib_id,
           '\t\t(at %s %s %d)' % (fmt(x), fmt(y), rot),
           '\t\t(unit 1)',
           '\t\t(exclude_from_sim no)', '\t\t(in_bom yes)', '\t\t(on_board yes)',
           '\t\t(dnp no)', '\t\t(fields_autoplaced yes)',
           '\t\t(uuid "%s")' % uid()]
    dy = -7.62
    for pname, pval, hide in props:
        dy += 2.54
        out.append('\t\t(property "%s" "%s"' % (pname, pval))
        out.append('\t\t\t(at %s %s 0)' % (fmt(x), fmt(y + dy)))
        if hide:
            out.append('\t\t\t(hide yes)')
        out.append('\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t)\n\t\t\t\t(justify left)\n\t\t\t)')
        out.append('\t\t)')
    for num in pin_numbers:
        out.append('\t\t(pin "%s"\n\t\t\t(uuid "%s")\n\t\t)' % (num, uid()))
    out.append('\t\t(instances\n\t\t\t(project "%s"\n\t\t\t\t(path "/%s"\n\t\t\t\t\t(reference "%s")\n\t\t\t\t\t(unit 1)\n\t\t\t\t)\n\t\t\t)\n\t\t)'
               % (project, root_uuid, ref))
    out.append('\t)')
    return "\n".join(out)


def emit_wire(x1, y1, x2, y2):
    return ('\t(wire\n\t\t(pts\n\t\t\t(xy %s %s)\n\t\t\t(xy %s %s)\n\t\t)\n'
            '\t\t(stroke\n\t\t\t(width 0)\n\t\t\t(type default)\n\t\t)\n'
            '\t\t(uuid "%s")\n\t)' % (fmt(x1), fmt(y1), fmt(x2), fmt(y2), uid()))


def emit_label(text, x, y, ang):
    just = "right bottom" if ang == 180 else "left bottom"
    a = 0 if ang == 180 else ang
    return ('\t(label "%s"\n\t\t(at %s %s %d)\n\t\t(fields_autoplaced yes)\n'
            '\t\t(effects\n\t\t\t(font\n\t\t\t\t(size 1.27 1.27)\n\t\t\t)\n'
            '\t\t\t(justify %s)\n\t\t)\n\t\t(uuid "%s")\n\t)'
            % (text, fmt(x), fmt(y), a, just, uid()))


def emit_nc(x, y):
    return '\t(no_connect\n\t\t(at %s %s)\n\t\t(uuid "%s")\n\t)' % (fmt(x), fmt(y), uid())


def emit_text(text, x, y, size=2.0):
    return ('\t(text "%s"\n\t\t(exclude_from_sim no)\n\t\t(at %s %s 0)\n'
            '\t\t(effects\n\t\t\t(font\n\t\t\t\t(size %s %s)\n\t\t\t)\n'
            '\t\t\t(justify left bottom)\n\t\t)\n\t\t(uuid "%s")\n\t)'
            % (text.replace('"', "'"), fmt(x), fmt(y), fmt(size), fmt(size), uid()))


# ---------------------------------------------------------------------------

def generate(tier):
    parts, meta = boarddef.build(tier)
    project = tier
    outdir = os.path.join(OUT_ROOT, tier)
    os.makedirs(outdir, exist_ok=True)
    root_uuid = uid()

    lib_syms = {}
    body = []
    pwr_counter = [0]

    def add_power(net, x, y, dx, dy):
        pwr_counter[0] += 1
        ref = "#PWR%04d" % pwr_counter[0]
        lib_id = POWER_NETS[net]
        if lib_id not in lib_syms:
            lib_syms[lib_id] = resolved_symbol(lib_id)
        table = PWR_ROT_GND if net == "GND" else PWR_ROT_RAIL
        rot = table.get((int(dx), int(dy)), 0)
        body.append(emit_symbol(lib_id, ref, net, "", x, y, rot, ["1"],
                                root_uuid, project, hide_value=True))

    for p in parts:
        if p.lib_id not in lib_syms:
            lib_syms[p.lib_id] = resolved_symbol(p.lib_id)
        sym_body, pins = lib_syms[p.lib_id]
        px, py = snap(p.sch[0]), snap(p.sch[1])
        body.append(emit_symbol(p.lib_id, p.ref, p.value, p.footprint,
                                px, py, 0, sorted(pins.keys()),
                                root_uuid, project))
        done_pts = {}
        for num, (pname, etype, lx, ly, ang) in sorted(pins.items()):
            net = p.pins.get(num)
            is_nc = num in p.nc
            if net is None and not is_nc:
                continue
            ax, ay = px + lx, py - ly
            dx, dy = outward(ang)
            key = (round(ax, 3), round(ay, 3))
            if key in done_pts:
                # Stacked pins (USB VBUS/GND) share one stub; nets must agree.
                if done_pts[key] != net:
                    raise ValueError("%s pin %s: stacked pins disagree (%s vs %s)"
                                     % (p.ref, num, done_pts[key], net))
                continue
            done_pts[key] = net
            if is_nc:
                body.append(emit_nc(ax, ay))
                continue
            ex, ey = ax + dx * 2.54, ay + dy * 2.54
            body.append(emit_wire(ax, ay, ex, ey))
            if net in POWER_NETS:
                add_power(net, ex, ey, dx, dy)
            else:
                body.append(emit_label(net, ex, ey, label_angle(dx, dy)))

    # Power flags so ERC sees each rail as driven.
    flag_lib = "power:PWR_FLAG"
    lib_syms[flag_lib] = resolved_symbol(flag_lib)
    fx = snap(30.0)
    for net in ("+5V", "GND"):
        pwr_counter[0] += 1
        ref = "#FLG%04d" % pwr_counter[0]
        body.append(emit_symbol(flag_lib, ref, "PWR_FLAG", "", fx, snap(400), 0,
                                ["1"], root_uuid, project, hide_value=True))
        # PWR_FLAG pin 1 sits at the symbol origin and points up.
        body.append(emit_wire(fx, snap(400), fx, snap(400) + 2.54))
        add_power(net, fx, snap(400) + 2.54, 0, 1)
        fx += snap(25.0)

    notes = [
        "%s  -  %s  (%d keys, rev %d)" % (meta["nick"], tier, meta["keys"], meta["rev"]),
        "Bottom-right key is push-to-talk on every tier; the rest are agent slots.",
        "Expander address A2:A1:A0 = 000 -> 0x20.  Firmware I2C: SDA=IO9, SCL=IO8.",
        "U2 symbol is PCF8575DBR standing in for an XL9555 in the same TSSOP-24 pinout - VERIFY.",
    ]
    ny = 30.0
    for line in notes:
        body.append(emit_text(line, 25.0, ny, 2.0 if line is notes[0] else 1.6))
        ny += 6.0

    header = ['(kicad_sch',
              '\t(version 20250114)',
              '\t(generator "switchboard-hw-gen")',
              '\t(generator_version "9.0")',
              '\t(uuid "%s")' % root_uuid,
              '\t(paper "A2")',
              '\t(title_block',
              '\t\t(title "%s - %s agent-status keypad")' % (meta["nick"], tier),
              '\t\t(date "%s")' % boarddef.BUILD_TAG,
              '\t\t(rev "r%d")' % meta["rev"],
              '\t\t(comment 1 "Generated by hardware/kicad/tools/gen_sch.py - edit the generator, not this file")',
              '\t)',
              '\t(lib_symbols']
    for lib_id in sorted(lib_syms):
        header.append(lib_syms[lib_id][0].rstrip("\n"))
    header.append('\t)')

    footer = ['\t(sheet_instances',
              '\t\t(path "/"',
              '\t\t\t(page "1")',
              '\t\t)',
              '\t)',
              '\t(embedded_fonts no)',
              ')', '']

    path = os.path.join(outdir, tier + ".kicad_sch")
    with open(path, "w") as fh:
        fh.write("\n".join(header) + "\n" + "\n".join(body) + "\n" +
                 "\n".join(footer))
    write_project(outdir, tier)
    return path, meta


PRO_TEMPLATE = """{
  "board": {
    "design_settings": {
      "defaults": {
        "board_outline_line_width": 0.1,
        "copper_line_width": 0.2,
        "copper_text_size_h": 1.0,
        "copper_text_size_v": 1.0,
        "copper_text_thickness": 0.15,
        "silk_line_width": 0.15,
        "silk_text_size_h": 1.0,
        "silk_text_size_v": 1.0,
        "silk_text_thickness": 0.15
      },
      "diff_pair_dimensions": [],
      "drc_exclusions": [],
      "rules": {
        "min_copper_edge_clearance": 0.3,
        "min_hole_clearance": 0.25,
        "min_resolved_spokes": 1,
        "min_through_hole_diameter": 0.2,
        "min_track_width": 0.2,
        "min_via_diameter": 0.45
      },
      "track_widths": [0.0, 0.25, 0.4, 0.8],
      "via_dimensions": []
    }
  },
  "boards": [],
  "libraries": {
    "pinned_footprint_libs": [],
    "pinned_symbol_libs": []
  },
  "meta": {
    "filename": "%(name)s.kicad_pro",
    "version": 3
  },
  "net_settings": {
    "classes": [
      {
        "bus_width": 12,
        "clearance": 0.2,
        "diff_pair_gap": 0.25,
        "diff_pair_width": 0.2,
        "line_style": 0,
        "microvia_diameter": 0.3,
        "microvia_drill": 0.1,
        "name": "Default",
        "pcb_color": "rgba(0, 0, 0, 0.000)",
        "schematic_color": "rgba(0, 0, 0, 0.000)",
        "track_width": 0.25,
        "via_diameter": 0.8,
        "via_drill": 0.4,
        "wire_width": 6
      },
      {
        "bus_width": 12,
        "clearance": 0.2,
        "diff_pair_gap": 0.25,
        "diff_pair_width": 0.2,
        "line_style": 0,
        "microvia_diameter": 0.3,
        "microvia_drill": 0.1,
        "name": "Power",
        "pcb_color": "rgba(0, 0, 0, 0.000)",
        "schematic_color": "rgba(0, 0, 0, 0.000)",
        "track_width": 0.6,
        "via_diameter": 0.9,
        "via_drill": 0.5,
        "wire_width": 6
      }
    ],
    "meta": {
      "version": 4
    },
    "netclass_assignments": {},
    "netclass_patterns": [
      { "netclass": "Power", "pattern": "GND" },
      { "netclass": "Power", "pattern": "+5V" },
      { "netclass": "Power", "pattern": "+3V3" }
    ]
  },
  "pcbnew": {
    "last_paths": {
      "gencad": "",
      "idf": "",
      "netlist": "",
      "plot": "",
      "pos_files": "",
      "specctra_dsn": "",
      "step": "",
      "svg": "",
      "vrml": ""
    },
    "page_layout_descr_file": ""
  },
  "schematic": {
    "legacy_lib_dir": "",
    "legacy_lib_list": []
  },
  "sheets": [],
  "text_variables": {}
}
"""


def write_project(outdir, name):
    with open(os.path.join(outdir, name + ".kicad_pro"), "w") as fh:
        fh.write(PRO_TEMPLATE % {"name": name})
    # Project-local library tables: the custom hot-swap footprint plus every
    # stock KiCad library this design pulls from, so the project opens (and
    # kicad-cli runs) without depending on the user's global tables.
    fp_libs = ["RF_Module", "Package_SO", "Package_TO_SOT_SMD", "Connector_USB",
               "Connector_PinHeader_2.54mm", "Resistor_SMD", "Capacitor_SMD",
               "Button_Switch_SMD", "LED_SMD"]
    lines = ['(fp_lib_table', '  (version 7)',
             '  (lib (name "switchboard")(type "KiCad")'
             '(uri "${KIPRJMOD}/../lib/switchboard.pretty")(options "")'
             '(descr "Switchboard keypad custom footprints"))']
    for lib in fp_libs:
        lines.append('  (lib (name "%s")(type "KiCad")'
                     '(uri "${KICAD10_FOOTPRINT_DIR}/%s.pretty")(options "")(descr ""))'
                     % (lib, lib))
    lines.append(')')
    with open(os.path.join(outdir, "fp-lib-table"), "w") as fh:
        fh.write("\n".join(lines) + "\n")

    sym_libs = ["RF_Module", "Interface_Expansion", "Regulator_Linear", "74xGxx",
                "Connector", "Connector_Generic", "Device", "Switch", "LED", "power"]
    lines = ['(sym_lib_table', '  (version 7)']
    for lib in sym_libs:
        lines.append('  (lib (name "%s")(type "KiCad")'
                     '(uri "${KICAD10_SYMBOL_DIR}/%s.kicad_sym")(options "")(descr ""))'
                     % (lib, lib))
    lines.append(')')
    with open(os.path.join(outdir, "sym-lib-table"), "w") as fh:
        fh.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    for tier in ("4key", "8key", "12key"):
        path, meta = generate(tier)
        print("wrote %s (%d keys, %.1f x %.1f mm)"
              % (path, meta["keys"], meta["w"], meta["h"]))
