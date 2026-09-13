#!/usr/bin/env python3
"""Generate the enclosure version-identification badge ("The Tally").

The easter-egg scheme lives on the 3D-printed case, not the PCB. This script
reads the same tier/revision data the PCB generator uses
(`hardware/kicad/tools/boarddef.py`) so the case and the board can never
disagree about which revision they are, and emits, per tier:

    <tier>_badge.scad   parametric OpenSCAD. Two modes: emboss (raised, for
                        union() into a case wall) and engrave (recessed, for
                        difference() out of one). Includes the text marks.
    <tier>_badge.stl    the embossed badge as a standalone printable tile, so
                        the geometry can be previewed/test-printed without
                        OpenSCAD installed. No text (STL has no font engine
                        here) - the .scad is the authority for that.

Plain python3; no OpenSCAD or CAD library required.
"""

import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.normpath(
    os.path.join(HERE, "..", "..", "kicad", "tools")))
import boarddef  # noqa: E402

# --- badge geometry (mm) ---------------------------------------------------
TILE_W, TILE_H, TILE_T = 70.0, 36.0, 2.0
RELIEF = 0.8          # how far marks stand proud of / sink into the surface
MORSE_ORIGIN = (5.0, 9.0)
TALLY_ORIGIN = (5.0, 14.0)
TALLY_H = 4.0
TALLY_RIB_W = 1.0
MASCOT_ORIGIN = (44.0, 6.0)
MASCOT_SCALE = 0.95
TEXT_ORIGIN = (5.0, 24.0)


# --- 2D helpers ------------------------------------------------------------

def _area2(p):
    s = 0.0
    for i in range(len(p)):
        x1, y1 = p[i]
        x2, y2 = p[(i + 1) % len(p)]
        s += x1 * y2 - x2 * y1
    return s / 2.0


def _cross(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _in_tri(p, a, b, c):
    d1, d2, d3 = _cross(a, b, p), _cross(b, c, p), _cross(c, a, p)
    neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
    pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
    return not (neg and pos)


def ear_clip(pts):
    """Triangulate a simple polygon (CCW) by ear clipping. Handles the
    non-convex shapes in the mascot, which a triangle fan would get wrong."""
    if _area2(pts) < 0:
        pts = list(reversed(pts))
    idx = list(range(len(pts)))
    tris = []
    guard = 0
    while len(idx) > 3 and guard < 5000:
        guard += 1
        clipped = False
        for k in range(len(idx)):
            i0, i1, i2 = idx[k - 1], idx[k], idx[(k + 1) % len(idx)]
            a, b, c = pts[i0], pts[i1], pts[i2]
            if _cross(a, b, c) <= 0:
                continue                      # reflex vertex, not an ear
            if any(_in_tri(pts[j], a, b, c)
                   for j in idx if j not in (i0, i1, i2)):
                continue                      # another vertex inside it
            tris.append((i0, i1, i2))
            idx.pop(k)
            clipped = True
            break
        if not clipped:
            break
    if len(idx) == 3:
        tris.append(tuple(idx))
    return pts, tris


def rect(cx, cy, w, h):
    return [(cx - w / 2.0, cy - h / 2.0), (cx + w / 2.0, cy - h / 2.0),
            (cx + w / 2.0, cy + h / 2.0), (cx - w / 2.0, cy + h / 2.0)]


def thick_segment(a, b, w):
    """A line segment as a rectangle polygon, so tally strokes become ribs."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    n = math.hypot(dx, dy) or 1.0
    ox, oy = -dy / n * w / 2.0, dx / n * w / 2.0
    return [(a[0] + ox, a[1] + oy), (a[0] - ox, a[1] - oy),
            (b[0] - ox, b[1] - oy), (b[0] + ox, b[1] + oy)]


# --- the marks -------------------------------------------------------------

def badge_polygons(tier):
    """Every mark on the badge, as 2D polygons in badge coords (y up).

    The PCB generator works in KiCad's y-down convention, so anything borrowed
    from boarddef gets flipped here.
    """
    meta = boarddef.TIERS[tier]
    polys = []

    # 1. Morse strip: the tier nickname, as a row of dots and dashes.
    mx, my = MORSE_ORIGIN
    for cx, cy, w, h in boarddef.morse_bars(meta["egg_word"], 0.0, 0.0,
                                            unit=0.9, gap=0.9,
                                            letter_gap=2.7):
        polys.append(rect(mx + cx, my - cy, w, h))

    # 2. Revision tally: raised ribs you can count with a thumb, in the dark,
    #    without turning the keypad over far enough to read anything.
    tx, ty = TALLY_ORIGIN
    for a, b in boarddef.tally_marks(boarddef.REV, 0.0, 0.0,
                                     h=TALLY_H, pitch=1.6):
        polys.append(thick_segment((tx + a[0], ty - a[1]),
                                   (tx + b[0], ty - b[1]), TALLY_RIB_W))

    # 3. Mascot: the carrot-and-stick scene, one stage further along per tier.
    ox, oy = MASCOT_ORIGIN
    k = MASCOT_SCALE
    top = max(y for pts in boarddef.mascot_polys("12key") for _, y in pts)
    for pts in boarddef.mascot_polys(tier):
        polys.append([(ox + x * k, oy + (top - y) * k) for x, y in pts])
    return polys


# --- STL -------------------------------------------------------------------

def prism(polys_out, poly, z0, z1):
    pts, tris = ear_clip(poly)
    for (i, j, k) in tris:
        a, b, c = pts[i], pts[j], pts[k]
        polys_out.append(((a[0], a[1], z1), (b[0], b[1], z1), (c[0], c[1], z1)))
        polys_out.append(((c[0], c[1], z0), (b[0], b[1], z0), (a[0], a[1], z0)))
    n = len(pts)
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        polys_out.append(((a[0], a[1], z0), (b[0], b[1], z0), (b[0], b[1], z1)))
        polys_out.append(((a[0], a[1], z0), (b[0], b[1], z1), (a[0], a[1], z1)))


def write_stl(path, tris, name):
    def norm(t):
        (ax, ay, az), (bx, by, bz), (cx, cy, cz) = t
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = cx - ax, cy - ay, cz - az
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        m = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
        return nx / m, ny / m, nz / m

    with open(path, "w") as fh:
        fh.write("solid %s\n" % name)
        for t in tris:
            nx, ny, nz = norm(t)
            fh.write("  facet normal %.6f %.6f %.6f\n    outer loop\n"
                     % (nx, ny, nz))
            for (x, y, z) in t:
                fh.write("      vertex %.4f %.4f %.4f\n" % (x, y, z))
            fh.write("    endloop\n  endfacet\n")
        fh.write("endsolid %s\n" % name)


def check_closed(tris):
    """Each volume should be closed: every directed edge matched by its
    reverse. Reported per run so a bad polygon cannot slip through silently."""
    edges = {}
    for t in tris:
        for i in range(3):
            a, b = t[i], t[(i + 1) % 3]
            ka = (tuple(round(v, 4) for v in a), tuple(round(v, 4) for v in b))
            edges[ka] = edges.get(ka, 0) + 1
    bad = 0
    for (a, b), c in edges.items():
        if edges.get((b, a), 0) != c:
            bad += 1
    return bad


# --- OpenSCAD --------------------------------------------------------------

SCAD = '''// %(nick)s - enclosure version badge, revision r%(rev)d (%(date)s)
//
// GENERATED by hardware/enclosure/tools/gen_enclosure_badge.py - edit the
// generator (or hardware/kicad/tools/boarddef.py), not this file.
//
// The scheme is "The Tally". Three marks, meant for the underside or the rear
// wall of the printed case:
//   * a Morse strip spelling this tier's nickname, which reads as decorative
//     ribbing until somebody notices the rhythm is irregular;
//   * a revision tally - %(rev)d stroke(s) = r%(rev)d - raised so it can be
//     counted by thumb without reading anything;
//   * the carrot-and-stick mascot, one stage further along for each tier up
//     the range: stick / stick + string / stick + string + carrot.
//
// Usage in your own case:
//   union()     { my_case(); badge(mode="emboss"); }   // raised marks
//   difference(){ my_case(); badge(mode="engrave"); }  // recessed marks
// Position it yourself with translate()/rotate(); the badge is drawn in the
// XY plane growing in +Z, origin at its lower-left corner.

tier      = "%(tier)s";
nickname  = "%(nick)s";
revision  = %(rev)d;
build_tag = "%(date)s";

tile_w = %(tw).1f;
tile_h = %(th).1f;
tile_t = %(tt).1f;
relief = %(relief).2f;   // raised height, or cut depth when engraving
text_marks = true;       // set false if your slicer/printer hates fine text

module marks() {
  linear_extrude(height = relief * 2)   // 2x so an engrave cut passes through
%(polys)s
  if (text_marks) {
    linear_extrude(height = relief * 2)
      translate([%(txx).1f, %(txy).1f]) text(nickname, size = 4.2, font = "Helvetica:style=Bold");
    linear_extrude(height = relief * 2)
      translate([%(txx).1f, %(txy2).1f]) text(str("~ ", tier, "  r", revision, "  ", build_tag, " ~"), size = 2.6);
  }
}

// mode = "emboss" -> raised marks sitting on the XY plane
// mode = "engrave" -> marks to subtract, sunk `relief` into the surface
module badge(mode = "emboss") {
  if (mode == "emboss") translate([0, 0, 0]) marks_clipped();
  else translate([0, 0, -relief]) marks();
}

module marks_clipped() {
  intersection() {
    marks();
    translate([-1, -1, 0]) cube([tile_w + 2, tile_h + 2, relief]);
  }
}

// Standalone tile, matching <tier>_badge.stl. Print this on its own to check
// the marks read at your layer height before committing to a whole case.
module badge_tile() {
  union() {
    cube([tile_w, tile_h, tile_t]);
    translate([0, 0, tile_t]) marks_clipped();
  }
}

badge_tile();
'''


def scad_polys(polys):
    lines = []
    for poly in polys:
        pts = ", ".join("[%.3f, %.3f]" % (x, y) for x, y in poly)
        lines.append("    polygon(points = [%s]);" % pts)
    return "\n".join(lines)


# --- main ------------------------------------------------------------------

def generate(tier):
    polys = badge_polygons(tier)
    meta = boarddef.TIERS[tier]

    tris = []
    prism(tris, rect(TILE_W / 2.0, TILE_H / 2.0, TILE_W, TILE_H), 0.0, TILE_T)
    for poly in polys:
        prism(tris, poly, TILE_T, TILE_T + RELIEF)
    stl = os.path.join(OUT, "%s_badge.stl" % tier)
    write_stl(stl, tris, "%s_badge_r%d" % (tier, boarddef.REV))

    scad = os.path.join(OUT, "%s_badge.scad" % tier)
    with open(scad, "w") as fh:
        fh.write(SCAD % {
            "tier": tier, "nick": meta["nick"], "rev": boarddef.REV,
            "date": boarddef.BUILD_TAG, "tw": TILE_W, "th": TILE_H,
            "tt": TILE_T, "relief": RELIEF, "polys": scad_polys(polys),
            "txx": TEXT_ORIGIN[0], "txy": TEXT_ORIGIN[1],
            "txy2": TEXT_ORIGIN[1] - 3.6,
        })
    return stl, scad, len(polys), len(tris), check_closed(tris)


if __name__ == "__main__":
    for tier in ("4key", "8key", "12key"):
        stl, scad, npolys, ntris, bad = generate(tier)
        print("%-6s %2d marks, %4d facets, unmatched edges: %d  -> %s, %s"
              % (tier, npolys, ntris, bad,
                 os.path.basename(stl), os.path.basename(scad)))
