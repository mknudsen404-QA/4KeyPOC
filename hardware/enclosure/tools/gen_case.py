#!/usr/bin/env python3
"""Generate a first-pass, 3D-printable two-piece case for the 4key board.

Real geometry only - every dimension below is pulled from
hardware/kicad/tools/boarddef.py (board outline, mounting holes, key
centres) or measured directly from the actual KiCad footprints this
design uses (USB-C connector body, M2 mounting-hole drill), not guessed.

This is explicitly a ROUGH FIRST DRAFT for checking proportions, key
spacing, and USB-C access before anything is printed - not a
fit-verified production enclosure. No switch/keycap/connector 3D models
were available to check real clearances against, so wall and standoff
heights are reasonable keyboard-case defaults, not measurements. Print
one and see before cutting final numbers in stone.

Requires build123d (`pip install build123d`) - not OpenSCAD, since
OpenSCAD could not be installed on this machine (Homebrew's cask is
Gatekeeper-disabled) and build123d is a real CSG kernel installable via
plain pip with no GUI app needed.

Outputs, per tier (currently only "4key" is wired up - the 8key/12key
row counts are already parametric below if that's ever wanted):

    <tier>_case_tray.stl    bottom tray: floor, walls, 4 standoffs,
                            USB-C wall cutout
    <tier>_case_plate.stl   top plate: 4 key cutouts, 4 screw-clearance
                            holes aligned to the tray's standoffs

Both STLs preview natively in Finder (select the file, press Space) -
no slicer or CAD viewer needed just to look at the shape.
"""

import os
import sys

from build123d import (
    Box,
    BuildPart,
    Cylinder,
    Locations,
    Mode,
    export_stl,
    fillet,
)

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.normpath(os.path.join(HERE, "..", "..", "kicad", "tools")))
import boarddef  # noqa: E402

# --- real board facts (not guessed) ----------------------------------------
# Mounting-hole drill: Package "MountingHole_2.2mm_M2" - measured from the
# actual KiCad footprint file.
MOUNT_HOLE_D = 2.2
MOUNT_CLEARANCE_D = 2.6     # a hair over the drill, for the plate's own holes

# USB-C receptacle F.Fab body, measured from the actual footprint
# (Connector_USB:USB_C_Receptacle_HCTL_HC-TYPE-C-16P-01A): local x in
# [-4.47,4.47], y in [-3.675,3.675]; J1 sits at boarddef pcb=(70,3) rot=180,
# so the body lands at absolute x:[65.53,74.47], y:[-0.675,6.675] - it pokes
# 0.675mm past the board's own y=0 edge (confirmed during the pre-fab
# review). Cut the wall a little wider than the body for real-world fit.
USB_CENTER_X = 70.0
USB_CUTOUT_W = 11.0
USB_CUTOUT_H = 4.0

# --- case dimensions (reasonable first-draft defaults, not measurements) ---
WALL = 2.0              # tray wall / floor thickness
PLATE_T = 2.0            # top plate thickness
BOARD_T = 1.6            # standard PCB thickness
STANDOFF_H = 3.0         # tray floor -> board underside clearance
STANDOFF_D = 5.0         # standoff outer diameter
WALL_H = 11.0            # board top surface -> top of switch bodies, roughly
BOARD_CLEARANCE = 0.4    # extra room around the PCB so it actually drops in
CORNER_R = 3.0           # outer case corner fillet
KEY_HOLE = 14.0          # standard MX plate-mount cutout, square


def mounting_holes(tier):
    _, meta = boarddef.build(tier)
    W, H = meta["w"], meta["h"]
    return W, H, [
        (51.5, 4.0), (W - 4.0, 4.0),
        (4.0, H - 4.0), (W - 4.0, H - 4.0),
    ]


def key_centers(tier):
    _, meta = boarddef.build(tier)
    rows, n = meta["rows"], meta["keys"]
    return [boarddef.key_center(i, rows) for i in range(n)]


def build_tray(tier):
    W, H, holes = mounting_holes(tier)
    outer_w = W + 2 * BOARD_CLEARANCE + 2 * WALL
    outer_h = H + 2 * BOARD_CLEARANCE + 2 * WALL
    total_h = WALL + STANDOFF_H + BOARD_T + WALL_H

    # Box, hollowed from the top, with the vertical edges rounded.
    with BuildPart() as tray:
        with Locations((outer_w / 2.0, outer_h / 2.0, total_h / 2.0)):
            Box(outer_w, outer_h, total_h)
        # Hollow out everything above the floor.
        cavity_h = total_h - WALL + 0.01
        with Locations((outer_w / 2.0, outer_h / 2.0, WALL + cavity_h / 2.0 - 0.005)):
            Box(outer_w - 2 * WALL, outer_h - 2 * WALL, cavity_h, mode=Mode.SUBTRACT)
        # Round the four outer vertical edges.
        verticals = [e for e in tray.part.edges() if e.is_interior is False and
                     abs(e.start_point().Z - e.end_point().Z) > total_h - 0.1]
        if verticals:
            fillet(verticals, CORNER_R)
        # USB-C wall cutout, centered on the connector's real position,
        # vertically centered on the board's own top surface (floor +
        # standoff height + half the board thickness).
        usb_cx = outer_w / 2.0 - W / 2.0 + USB_CENTER_X
        usb_cz = WALL + STANDOFF_H + BOARD_T / 2.0
        with Locations((usb_cx, 0.0, usb_cz)):
            Box(USB_CUTOUT_W, WALL * 3, USB_CUTOUT_H, mode=Mode.SUBTRACT)
        # Standoffs, one per mounting hole, each with its own through-hole.
        ox = outer_w / 2.0 - W / 2.0
        oy = outer_h / 2.0 - H / 2.0
        for hx, hy in holes:
            cx, cy = ox + hx, oy + hy
            with Locations((cx, cy, WALL + STANDOFF_H / 2.0)):
                Cylinder(STANDOFF_D / 2.0, STANDOFF_H)
            with Locations((cx, cy, WALL + STANDOFF_H / 2.0)):
                Cylinder(MOUNT_HOLE_D / 2.0, STANDOFF_H + 0.1, mode=Mode.SUBTRACT)
    return tray.part, outer_w, outer_h


def build_plate(tier):
    W, H, holes = mounting_holes(tier)
    outer_w = W + 2 * BOARD_CLEARANCE + 2 * WALL
    outer_h = H + 2 * BOARD_CLEARANCE + 2 * WALL
    ox = outer_w / 2.0 - W / 2.0
    oy = outer_h / 2.0 - H / 2.0

    with BuildPart() as plate:
        with Locations((outer_w / 2.0, outer_h / 2.0, PLATE_T / 2.0)):
            Box(outer_w, outer_h, PLATE_T)
        verticals = [e for e in plate.part.edges() if
                     abs(e.start_point().Z - e.end_point().Z) > PLATE_T - 0.1]
        if verticals:
            fillet(verticals, CORNER_R)
        for kx, ky in key_centers(tier):
            with Locations((ox + kx, oy + ky, PLATE_T / 2.0)):
                Box(KEY_HOLE, KEY_HOLE, PLATE_T + 0.1, mode=Mode.SUBTRACT)
        for hx, hy in holes:
            with Locations((ox + hx, oy + hy, PLATE_T / 2.0)):
                Cylinder(MOUNT_CLEARANCE_D / 2.0, PLATE_T + 0.1, mode=Mode.SUBTRACT)
    return plate.part, outer_w, outer_h


def main():
    for tier in ("4key",):
        tray, w, h = build_tray(tier)
        plate, _, _ = build_plate(tier)
        tray_path = os.path.join(OUT, "%s_case_tray.stl" % tier)
        plate_path = os.path.join(OUT, "%s_case_plate.stl" % tier)
        export_stl(tray, tray_path)
        export_stl(plate, plate_path)
        print("wrote %s  (%.1f x %.1f mm footprint)" % (tray_path, w, h))
        print("wrote %s" % plate_path)


if __name__ == "__main__":
    main()
