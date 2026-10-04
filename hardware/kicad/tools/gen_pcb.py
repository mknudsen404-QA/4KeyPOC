#!/usr/bin/env python3
"""Generate <tier>.kicad_pcb from boarddef.py, using KiCad's own pcbnew API.

Run with KiCad's bundled interpreter, not the system one:

    /Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/\\
        Versions/3.9/bin/python3.9 gen_pcb.py

What this generator does and does not route is documented in
hardware/kicad/README.md - read that before assuming a net is finished.
"""

import os
import sys

import pcbnew

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import boarddef  # noqa: E402

FP_ROOT = "/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints"
LOCAL_FP = os.path.normpath(os.path.join(HERE, "..", "lib"))
OUT_ROOT = os.path.normpath(os.path.join(HERE, ".."))

ORIGIN = (100.0, 100.0)   # board local (0,0) lands here in KiCad page coords

TRACK = 0.25
TRACK_PWR = 0.6
VIA_D, VIA_DRILL = 0.8, 0.4


def mm(v):
    return pcbnew.FromMM(float(v))


def P(x, y):
    """Board-local mm -> KiCad VECTOR2I."""
    return pcbnew.VECTOR2I(mm(ORIGIN[0] + x), mm(ORIGIN[1] + y))


def fp_path(libname):
    if libname == "switchboard":
        return os.path.join(LOCAL_FP, "switchboard.pretty")
    return os.path.join(FP_ROOT, libname + ".pretty")


# ---------------------------------------------------------------------------

class BoardBuilder(object):
    def __init__(self, tier):
        self.tier = tier
        self.parts, self.meta = boarddef.build(tier)
        self.W = self.meta["w"]
        self.H = self.meta["h"]
        self.rows = self.meta["rows"]
        self.n = self.meta["keys"]
        self.board = pcbnew.BOARD()
        self.nets = {}
        self.fps = {}

    # -- infrastructure ----------------------------------------------------

    def net(self, name):
        if name not in self.nets:
            ni = pcbnew.NETINFO_ITEM(self.board, name)
            self.board.Add(ni)
            self.nets[name] = ni
        return self.nets[name]

    def place_parts(self):
        for p in self.parts:
            lib, name = p.footprint.split(":", 1)
            fp = pcbnew.FootprintLoad(fp_path(lib), name)
            if fp is None:
                raise RuntimeError("footprint %s not found" % p.footprint)
            fp.SetPosition(P(*p.pcb))
            if p.rot:
                fp.SetOrientationDegrees(p.rot)
            fp.SetReference(p.ref)
            fp.SetValue(p.value)
            fp.Value().SetVisible(False)
            if p.ref[0] not in ("U", "J"):
                fp.Reference().SetLayer(pcbnew.F_Fab)
            fp.Reference().SetVisible(True)
            self.board.Add(fp)
            self.fps[p.ref] = fp
            for pad in fp.Pads():
                num = pad.GetNumber()
                if num in p.nc:
                    # Must win over p.pins, same as gen_sch.py's is_nc check -
                    # a pin can have both a plausible net name (e.g. the last
                    # LED's DOUT, still called LEDCH%d) and be explicitly
                    # unconnected because there's nothing next in the chain.
                    # Without this the schematic (which does respect nc) and
                    # the PCB (which didn't) silently disagreed about whether
                    # that pin was wired - a real drift the "one definition
                    # drives both generators" design is supposed to prevent.
                    continue
                netname = p.pins.get(num)
                if netname:
                    pad.SetNet(self.net(netname))

    def pad(self, ref, num):
        fp = self.fps[ref]
        for p in fp.Pads():
            if p.GetNumber() == num:
                pos = p.GetPosition()
                return (pcbnew.ToMM(pos.x) - ORIGIN[0],
                        pcbnew.ToMM(pos.y) - ORIGIN[1])
        raise KeyError("%s pad %s" % (ref, num))

    # -- primitives --------------------------------------------------------

    def track(self, net, pts, width=TRACK, layer=None):
        layer = pcbnew.F_Cu if layer is None else layer
        for a, b in zip(pts, pts[1:]):
            if a == b:
                continue
            t = pcbnew.PCB_TRACK(self.board)
            t.SetStart(P(*a))
            t.SetEnd(P(*b))
            t.SetWidth(mm(width))
            t.SetLayer(layer)
            t.SetNet(self.net(net))
            self.board.Add(t)

    def via(self, net, x, y, d=VIA_D, drill=VIA_DRILL):
        v = pcbnew.PCB_VIA(self.board)
        v.SetPosition(P(x, y))
        v.SetViaType(pcbnew.VIATYPE_THROUGH)
        v.SetWidth(mm(d))
        v.SetDrill(mm(drill))
        v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
        v.SetNet(self.net(net))
        self.board.Add(v)

    def seg(self, layer, a, b, width=0.15):
        s = pcbnew.PCB_SHAPE(self.board)
        s.SetShape(pcbnew.SHAPE_T_SEGMENT)
        s.SetStart(P(*a))
        s.SetEnd(P(*b))
        s.SetLayer(layer)
        s.SetWidth(mm(width))
        self.board.Add(s)
        return s

    def poly(self, layer, pts, filled=True):
        s = pcbnew.PCB_SHAPE(self.board)
        s.SetShape(pcbnew.SHAPE_T_POLY)
        vec = pcbnew.VECTOR_VECTOR2I()
        for x, y in pts:
            vec.append(P(x, y))
        s.SetPolyPoints(vec)
        s.SetLayer(layer)
        s.SetFilled(filled)
        s.SetWidth(mm(0.0 if filled else 0.15))
        self.board.Add(s)
        return s

    def text(self, layer, s, x, y, size=1.2, thick=0.2, angle=0, mirror=False,
             just=None):
        t = pcbnew.PCB_TEXT(self.board)
        t.SetText(s)
        t.SetPosition(P(x, y))
        t.SetLayer(layer)
        t.SetTextSize(pcbnew.VECTOR2I(mm(size), mm(size)))
        t.SetTextThickness(mm(thick))
        if angle:
            t.SetTextAngleDegrees(angle)
        t.SetMirrored(mirror)
        if just is not None:
            t.SetHorizJustify(just)
        self.board.Add(t)
        return t

    # -- board shape -------------------------------------------------------

    def outline(self):
        r = 2.0     # corner radius
        W, H = self.W, self.H
        pts = [((r, 0), (W - r, 0)), ((W, r), (W, H - r)),
               ((W - r, H), (r, H)), ((0, H - r), (0, r))]
        for a, b in pts:
            self.seg(pcbnew.Edge_Cuts, a, b, 0.1)
        arcs = [((r, r), 180, 270), ((W - r, r), 270, 0),
                ((W - r, H - r), 0, 90), ((r, H - r), 90, 180)]
        import math
        for (cx, cy), a0, a1 in arcs:
            steps = 8
            prev = None
            for i in range(steps + 1):
                ang = math.radians(a0 + (a1 - a0) * i / float(steps))
                pt = (cx + r * math.cos(ang), cy + r * math.sin(ang))
                if prev:
                    self.seg(pcbnew.Edge_Cuts, prev, pt, 0.1)
                prev = pt

    def mounting_holes(self):
        fp = None
        # NOTE: not a symmetric rectangle. The top-left corner is inside the
        # ESP32 antenna keepout and the left strip is full of parts, so the
        # upper-left hole sits at x=51.5 instead. Revisit once an enclosure
        # exists (hardware/enclosure/ has only the version-badge tile today,
        # no case model to check this against - see its own README).
        for (x, y) in [(51.5, 4.0), (self.W - 4.0, 4.0),
                       (4.0, self.H - 4.0), (self.W - 4.0, self.H - 4.0)]:
            fp = pcbnew.FootprintLoad(fp_path("MountingHole"),
                                      "MountingHole_2.2mm_M2")
            fp.SetPosition(P(x, y))
            fp.SetReference("H%d" % (len(self.board.Footprints()) + 1))
            fp.Reference().SetVisible(False)
            fp.Value().SetVisible(False)
            self.board.Add(fp)

    # -- copper ------------------------------------------------------------

    ANT_W, ANT_H = 48.0, 21.0     # ESP32 PCB-antenna keepout (top-left corner)

    def gnd_zones(self):
        """GND pour on both layers, notched clear of the module antenna."""
        inset = 0.5
        W, H = self.W - inset, self.H - inset
        aw, ah = self.ANT_W, self.ANT_H
        outline = [(aw, inset), (W, inset), (W, H), (inset, H),
                   (inset, ah), (aw, ah)]
        for layer in (pcbnew.F_Cu, pcbnew.B_Cu):
            z = pcbnew.ZONE(self.board)
            z.SetLayer(layer)
            z.SetNet(self.net("GND"))
            z.SetLocalClearance(mm(0.25))
            z.SetMinThickness(mm(0.2))
            z.SetThermalReliefGap(mm(0.3))
            z.SetThermalReliefSpokeWidth(mm(0.4))
            z.SetIsFilled(False)
            pts = pcbnew.VECTOR_VECTOR2I()
            for x, y in outline:
                pts.append(P(x, y))
            z.AddPolygon(pts)
            self.board.Add(z)

    def stitching_vias(self):
        spots = [(10.0, self.H - 2.6), (30.0, self.H - 2.6),
                 (50.0, self.H - 2.6), (70.0, self.H - 2.6),
                 (86.0, 12.0), (86.0, 24.0), (86.0, 38.0), (2.0, 34.0)]
        for x, y in spots:
            self.via("GND", x, y)

    # -- the per-key LED subsystem (the part that IS routed) ---------------

    LEFT_CH = 3.6      # left margin channel: LED data between rows
    RIGHT_CH = 83.5    # right margin channel: +5V trunk

    def route_leds(self):
        rows = self.rows
        n = self.n
        bus_y = []
        # +5V bus per key row, sitting in the gap below the LEDs.
        for r in range(rows):
            _, ky = boarddef.key_center(r * boarddef.COLS, rows)
            y = ky + 8.8
            bus_y.append(y)
            kx_first = boarddef.key_center(r * boarddef.COLS, rows)[0]
            self.track("+5V", [(kx_first - 7.9, y), (self.RIGHT_CH, y)],
                       TRACK_PWR)
        # Right-margin +5V trunk joins the row buses and climbs to the strip.
        self.track("+5V", [(self.RIGHT_CH, bus_y[0]),
                           (self.RIGHT_CH, bus_y[-1])], TRACK_PWR)

        for i in range(n):
            kx, ky = boarddef.key_center(i, rows)
            r = i // boarddef.COLS
            y = bus_y[r]
            # +5V into the LED's VDD (left-bottom pad with the LED at rot 180)
            self.track("+5V", [(kx - 3.5, y), (kx - 3.5, ky + 5.875),
                               (kx - 1.75, ky + 5.875)], TRACK)
            # +5V into the per-key decoupling cap
            self.track("+5V", [(kx - 7.9, y), (kx - 7.9, ky + 7.6)], TRACK)
            # Data chain
            if i == n - 1:
                continue
            if (i + 1) % boarddef.COLS:
                # next LED is to the right, same row
                self.track("LEDCH%d" % (i + 1),
                           [(kx + 1.75, ky + 5.875),
                            (kx + 12.0, ky + 5.875),
                            (kx + 12.0, ky + 4.125),
                            (kx + 17.3, ky + 4.125)], TRACK)
            else:
                # Wrap to the start of the next row. This one dives to B.Cu:
                # the row's +5V bus already owns the F.Cu lane it would have
                # to cross on the way down.
                nkx, nky = boarddef.key_center(i + 1, rows)
                net = "LEDCH%d" % (i + 1)
                jog = kx + 4.0
                self.track(net, [(kx + 1.75, ky + 5.875), (jog, ky + 5.875)],
                           TRACK)
                self.via(net, jog, ky + 5.875)
                self.track(net, [(jog, ky + 5.875), (jog, ky + 9.8),
                                 (self.LEFT_CH, ky + 9.8),
                                 (self.LEFT_CH, nky + 4.125)],
                           TRACK, pcbnew.B_Cu)
                self.via(net, self.LEFT_CH, nky + 4.125)
                self.track(net, [(self.LEFT_CH, nky + 4.125),
                                 (nkx - 1.75, nky + 4.125)], TRACK)

        # Chain head: R7 -> D1 DIN, down the left margin.
        r7x, r7y = self.pad("R7", "2")
        _, ky0 = boarddef.key_center(0, rows)
        kx0, _ = boarddef.key_center(0, rows)
        self.track("LEDCH0", [(r7x, r7y), (r7x, 44.0), (self.LEFT_CH, 44.0),
                              (self.LEFT_CH, ky0 + 4.125),
                              (kx0 - 1.75, ky0 + 4.125)], TRACK)

    def route_strip(self):
        """The electronics strip.

        Routing budget on a 2-layer board: F.Cu carries the strip signals, and
        the three nets that would otherwise have to cross everything (3V3 to
        the left-hand region, SDA, SCL) dive to B.Cu and cross underneath the
        ground pour, which simply flows around them.
        """
        p = self.pad
        bus0 = boarddef.key_center(0, self.rows)[1] + 8.8

        # --- +5V ------------------------------------------------------------
        vb_l, vb_r = p("J1", "B9"), p("J1", "A9")   # the two VBUS pad columns
        c8, c1, c2, c7 = p("C8", "1"), p("C1", "1"), p("C2", "1"), p("C7", "1")
        self.track("+5V", [vb_r, (vb_r[0], 9.0), (c8[0], 9.0), c8], 0.4)
        self.track("+5V", [vb_l, (vb_l[0], 9.0), (vb_r[0], 9.0)], 0.4)
        self.track("+5V", [(51.5, 9.0), (vb_l[0], 9.0)], TRACK_PWR)
        self.track("+5V", [(c2[0], 9.0), c2], TRACK)
        # right-margin trunk down to the per-row LED buses
        self.track("+5V", [(self.RIGHT_CH, 9.0), (c8[0], 9.0)], TRACK_PWR)
        self.track("+5V", [(self.RIGHT_CH, 9.0), (self.RIGHT_CH, bus0)], TRACK_PWR)
        # LDO input + enable, tapped off the x=51.5 riser
        self.track("+5V", [(51.5, 9.0), (51.5, 22.5)], TRACK_PWR)
        self.track("+5V", [c1, (51.5, c1[1])], TRACK)
        u3_vin, u3_en = p("U3", "1"), p("U3", "3")
        self.track("+5V", [(51.5, u3_vin[1]), u3_vin], TRACK)
        self.track("+5V", [(51.5, u3_en[1]), u3_en], TRACK)

        # --- +3V3 -----------------------------------------------------------
        u3_vout, c3, c5 = p("U3", "5"), p("C3", "1"), p("C5", "1")
        u2_vcc = p("U2", "24")
        # This vertical F.Cu trunk at x=73 is the spine the whole I2C-expander
        # corner's +3V3 hangs off of - R4/R5/R6 (see route_remaining) tap it
        # directly, so it runs all the way down past R6, not just to U2.
        r6_1 = p("R6", "1")
        trunk_bottom = r6_1[1]
        self.track("+3V3", [u3_vout, (73.0, u3_vout[1]), (73.0, trunk_bottom)],
                   TRACK)
        self.track("+3V3", [(73.0, u2_vcc[1]), u2_vcc], TRACK)
        self.track("+3V3", [c3, (c3[0], u3_vout[1])], TRACK)
        self.track("+3V3", [(73.0, c5[1]), c5], TRACK)
        # 3V3 crosses to the left-hand region on the back layer.
        u1_3v3, c4, c9 = p("U1", "2"), p("C4", "1"), p("C9", "1")
        self.via("+3V3", 73.0, 25.5)
        self.via("+3V3", 13.0, 25.5)
        self.track("+3V3", [(73.0, 25.5), (13.0, 25.5)], TRACK, pcbnew.B_Cu)
        self.track("+3V3", [(13.0, 25.5), (13.0, u1_3v3[1]), u1_3v3], TRACK)
        self.track("+3V3", [(13.0, u1_3v3[1]), (c4[0], u1_3v3[1]), c4], TRACK)
        # C9: a second bulk cap on U1's own +3V3 net - not literally at the
        # pin (the whole left margin, x<20, is already packed solid: U1's
        # own body, C4, SW_BOOT, C7, U4, C6, SW_RST, R7 and their traces -
        # every spot tried there either crossed LED_GPIO's run through that
        # corner or sat on top of U1's own closely-pitched pin column at
        # x=15.25). Placed in the open bay right of U1 instead, well short
        # of C3's 60mm-away bulk cap, by tapping straight into the existing
        # (73.0,25.5)-(13.0,25.5) B.Cu +3V3 trunk at this x - a plain T-tap
        # onto copper already proven net-clean, not a new crossing. GND
        # side needs no explicit track, same as every other decoupling cap
        # here - it reaches the pour directly.
        c9_via = (c9[0], 25.5)
        self.via("+3V3", *c9_via)
        self.track("+3V3", [c9_via, c9], TRACK)

        # --- I2C, on the back layer ----------------------------------------
        # U2 pins 22/23/24 (SCL/SDA/+3V3) are a tight 0.65mm-pitch column
        # with R4/R5 (their own pull-ups) immediately beside them on F.Cu -
        # an Opus review (2026-10-03, pre-fab) caught the earlier version
        # of this (x=67/69 F.Cu columns) hard-shorting SCL/SDA into +3V3
        # and into each other once U2 moved down next to R4/R5. Both nets
        # stay on B.Cu from their U1-side via down to a staging column
        # (x=63.5 SCL, x=62.0 SDA - 1.5mm apart, clear of each other and
        # of U2's own west pin column at x=59.1), where the via back to
        # F.Cu sits - NOT at the pad itself: an 0.8mm via dropped directly
        # on a 0.65mm-pitch pad clips the neighbouring pin's pad every
        # time. A short F.Cu stub (~1.3-2.9mm) carries the last hop from
        # the via into the actual pad, at the pad's own y only, so it
        # never crosses another pin's y at this x.
        scl_u1, sda_u1 = p("U1", "12"), p("U1", "17")
        scl_u2, sda_u2 = p("U2", "22"), p("U2", "23")
        self.track("SCL", [scl_u1, (13.5, scl_u1[1])], TRACK)
        self.via("SCL", 13.5, scl_u1[1])
        self.track("SCL", [(13.5, scl_u1[1]), (13.5, 33.5), (66.1, 33.5),
                           (66.1, scl_u2[1])], TRACK, pcbnew.B_Cu)
        # Small via: the gap between U2's own pad tips (x=65.6) and R4's
        # pad1 (x=66.6) is only ~1mm, too narrow for the board's standard
        # 0.8mm via - a 0.45mm one just fits.
        self.via("SCL", 66.1, scl_u2[1], d=0.5, drill=0.25)
        self.track("SCL", [(66.1, scl_u2[1]), scl_u2], TRACK)

        self.track("SDA", [sda_u1, (sda_u1[0], 42.0)], TRACK)
        self.via("SDA", sda_u1[0], 42.0)
        self.track("SDA", [(sda_u1[0], 42.0), (sda_u1[0], 38.0), (62.0, 38.0),
                           (62.0, sda_u2[1])], TRACK, pcbnew.B_Cu)
        self.via("SDA", 62.0, sda_u2[1])
        self.track("SDA", [(62.0, sda_u2[1]), sda_u2], TRACK)

        # --- LED data: IO5 -> level shifter -> series resistor -------------
        gpio, u4_in = p("U1", "5"), p("U4", "2")
        self.track("LED_GPIO", [gpio, (13.2, gpio[1]), (13.2, 27.0),
                                (1.8, 27.0), (1.8, u4_in[1]), u4_in], TRACK)
        u4_vcc = p("U4", "5")
        self.track("+5V", [u4_vcc, (c7[0], u4_vcc[1]), c7], TRACK)
        self.track("+5V", [(51.5, 22.5), (51.5, 32.5)], TRACK_PWR)
        self.via("+5V", 51.5, 32.5)
        self.via("+5V", c7[0], 32.5)
        self.track("+5V", [(51.5, 32.5), (c7[0], 32.5)], TRACK, pcbnew.B_Cu)
        self.track("+5V", [(c7[0], 32.5), c7], TRACK)
        u4_out, r7_in = p("U4", "4"), p("R7", "1")
        self.track("LED_BUF", [u4_out, (7.0, u4_out[1]), (7.0, r7_in[1]),
                               r7_in], TRACK)

    # -- the nets the README deliberately left for a human ------------------
    #
    # KEY1..KEYn, USB_DP/USB_DM, CC1/CC2, ~RESET/BOOT, EXP_INT + its pull-up,
    # the I2C/EN pull-ups, and J2's header nets - closed out now that an
    # actual board order is imminent. Only implemented for a single key row
    # (4key's own layout): 8key/12key's multi-row key-to-expander routing is
    # a materially different, harder layout problem and is not attempted
    # here - those two tiers keep these nets unrouted, same as before.

    def route_remaining(self):
        if self.rows != 1:
            return
        p = self.pad

        # --- CC1 / CC2 ---------------------------------------------------
        # R2/R3 (and their pin-2 GND pads) sit in one tight row at y=12;
        # approaching pin 1 along that same row would cross the other
        # resistor's pins. Dropped below the row (clear of both bodies)
        # on two DIFFERENT staging rows - CC1 and CC2 can't share one,
        # since each one's row would then cross the other net's final
        # vertical hop into its own pin.
        j1_cc1, r2_1 = p("J1", "A5"), p("R2", "1")
        self.track("CC1", [j1_cc1, (j1_cc1[0], 14.0), (r2_1[0], 14.0),
                           r2_1], TRACK)
        j1_cc2, r3_1 = p("J1", "B5"), p("R3", "1")
        self.track("CC2", [j1_cc2, (j1_cc2[0], 15.0), (r3_1[0], 15.0),
                           r3_1], TRACK)

        # --- ~RESET / BOOT: explicit pad-instance x's -----------------------
        # SW_BOOT and SW_RST sit in the same x column (board-local x~5, one
        # above the other), and each switch's footprint has pin 1 split
        # into two physically separate pads (both on the same net) at
        # x=2.95 and x=7.05 - `pad()` returns whichever one it finds first,
        # which for both switches turned out to be the same instance,
        # putting BOOT and ~RESET's own traces on top of each other where
        # their paths met. Pinning BOOT to the x=7.05 pad and ~RESET to the
        # x=2.95 pad (both valid, same net either way) keeps them apart for
        # their whole run instead of just avoiding each other's footprint.
        rst_y = p("SW_RST", "1")[1]
        boot_y = p("SW_BOOT", "1")[1]
        u1_rst, u1_boot = p("U1", "3"), p("U1", "27")
        c6_1, r1_2 = p("C6", "1"), p("R1", "2")

        self.track("~RESET", [(2.95, rst_y), (2.95, c6_1[1]), c6_1], TRACK)
        self.track("~RESET", [c6_1, (r1_2[0], c6_1[1]), r1_2], TRACK)
        # R1 pin2 -> U1 pin3: crosses the +3V3 spine (below) on B.Cu at
        # y=22 - in the open band between the antenna keepout (ends 21.15)
        # and U2's body (starts 28.1), clear of U2 and of every other
        # B.Cu row this method uses. x=14 for the vertical legs, clear of
        # both the existing +3V3 stub (x=13) and U1's own pin column
        # (x=15.25) - a vertical at either of those x's would cross
        # existing copper or U1's own pads.
        self.track("~RESET", [r1_2, (r1_2[0], 22.0)], TRACK)
        self.via("~RESET", r1_2[0], 22.0)
        self.track("~RESET", [(r1_2[0], 22.0), (14.0, 22.0)],
                   TRACK, pcbnew.B_Cu)
        self.via("~RESET", 14.0, 22.0)
        self.track("~RESET", [(14.0, 22.0), (14.0, u1_rst[1]), u1_rst], TRACK)

        # BOOT's straight-down path at x=7.05 crosses both LED_GPIO's
        # horizontal run (y=27, x 1.8-13.2) and LEDCH0's own column
        # (x~3.6-10.4) on its way down - jogs right to x=12.5 immediately,
        # at y=21.5 (just inside the open band past the antenna keepout,
        # clear of C4 and of EXP_INT's via at x=10), before either of
        # those is reached.
        self.track("BOOT", [(7.05, boot_y), (7.05, 21.5), (12.5, 21.5),
                            (12.5, 39.15), (u1_boot[0], 39.15), u1_boot], TRACK)

        # --- R1 pin 1: +3V3, tapped off the existing B.Cu trunk at y=25.5
        # (U1's own side of the board - unrelated to the U2-corner spine
        # below).
        r1_1 = p("R1", "1")
        self.via("+3V3", r1_1[0], 25.5)
        self.track("+3V3", [(13.0, 25.5), (r1_1[0], 25.5)], TRACK, pcbnew.B_Cu)
        self.track("+3V3", [(r1_1[0], 25.5), r1_1], TRACK)

        # --- R4/R5/R6 pin 1: each taps the x=73 +3V3 spine directly
        # (route_strip runs it the full height of this corner, down past
        # R6, specifically so this is a short, independent hop per
        # resistor instead of one shared column - R4/R5/R6 are no longer
        # colinear the way they were before this corner got more room.
        r4_1, r5_1, r6_1 = p("R4", "1"), p("R5", "1"), p("R6", "1")
        self.track("+3V3", [r4_1, (73.0, r4_1[1])], TRACK)
        self.track("+3V3", [r5_1, (73.0, r5_1[1])], TRACK)
        self.track("+3V3", [r6_1, (73.0, r6_1[1])], TRACK)

        # --- SDA / SCL pull-ups (R4 pin2 / R5 pin2): tapped onto the same
        # B.Cu staging vias route_strip's U1<->U2 bus already drops at
        # (62.0, sda_u2_y) and (63.5, scl_u2_y) - not routed all the way
        # back to U1's own pad - much shorter. On F.Cu, R4/R5 pin1 (+3V3)
        # sits on the same row as pin2 (SDA/SCL respectively), so a
        # same-layer tap toward U2 would graze pin1's own pad; B.Cu has no
        # R4/R5 copper at all, so the whole hop happens there instead.
        r4_2, r5_2 = p("R4", "2"), p("R5", "2")
        u2_sda, u2_scl = p("U2", "23"), p("U2", "22")
        self.via("SDA", *r4_2)
        self.track("SDA", [r4_2, (62.0, u2_sda[1])], TRACK, pcbnew.B_Cu)
        self.via("SCL", *r5_2)
        self.track("SCL", [r5_2, (66.1, u2_scl[1])], TRACK, pcbnew.B_Cu)

        # --- EXP_INT: U1 pin4 -> U2 pin1, then on to R6 pin2 ---------------
        # U1 side: crosses on B.Cu at y=24 (x=10, clear of the existing
        # +3V3 stub at x=13 and of U1's own pin column at x=15.25) - above
        # U2's body (y>=28.1) and C9's own B.Cu hop (x=41), clear of both.
        u1_int, u2_int = p("U1", "4"), p("U2", "1")
        self.track("EXP_INT", [u1_int, (10.0, u1_int[1])], TRACK)
        self.via("EXP_INT", 10.0, u1_int[1])
        self.track("EXP_INT", [(10.0, u1_int[1]), (10.0, 24.0)],
                   TRACK, pcbnew.B_Cu)
        self.via("EXP_INT", 10.0, 24.0)
        self.track("EXP_INT", [(10.0, 24.0), (u2_int[0], 24.0), u2_int], TRACK)
        # U2 pin1 on to R6 pin2: these two now sit close together (R6 was
        # moved right next to U2's corner along with R4/R5 - see
        # boarddef.py), so this is a direct drop-then-jog instead of the
        # detour the old, more cramped layout needed.
        r6_2 = p("R6", "2")
        self.track("EXP_INT", [u2_int, (u2_int[0], r6_2[1]), r6_2], TRACK)
        # R6 pin1 (+3V3) is handled above, with R4/R5's own taps.

        # --- J2 header: HDR_* nets -----------------------------------------
        # SDA/SCL already reach J2 via the pour/existing I2C nets. Each
        # remaining net crosses on B.Cu at its OWN target pin's y (not a
        # shared staging row - a first attempt using one row per net still
        # put each one's final vertical hop at its real target x, and two
        # pairs of these six nets land on the very same U1 pin-column x
        # (HDR_IO6/HDR_IO7 both at x=15.25; HDR_IO38/HDR_TXD0/HDR_RXD0 all
        # at x=32.75), so those vertical hops overlapped each other). Nets
        # sharing a destination column instead enter it from a slightly
        # offset x, with only a short final horizontal at their own y into
        # the real pad - the one point they share is the pad itself, not
        # an extended run.
        hdr = {"HDR_IO38": ("7", "31", -1.5), "HDR_IO6": ("8", "6", -1.5),
               "HDR_IO7": ("9", "7", 1.5), "HDR_IO10": ("10", "18", 0.0),
               "HDR_TXD0": ("11", "37", 0.0), "HDR_RXD0": ("12", "36", 1.5)}
        for net, (j2_pin, u1_pin, dx) in hdr.items():
            j2_pad, u1_pad = p("J2", j2_pin), p("U1", u1_pin)
            target_y = u1_pad[1]
            entry_x = u1_pad[0] + dx
            self.track(net, [j2_pad, (j2_pad[0], target_y)], TRACK)
            self.via(net, j2_pad[0], target_y)
            self.track(net, [(j2_pad[0], target_y), (entry_x, target_y)],
                       TRACK, pcbnew.B_Cu)
            self.via(net, entry_x, target_y)
            self.track(net, [(entry_x, target_y), u1_pad], TRACK)

        # --- USB_DP / USB_DM: J1 -> D_ESD -> U1, the one differential pair
        # Kept as a tightly-coupled parallel pair at constant spacing the
        # whole way, which is what actually matters for signal integrity
        # here, more than hitting a specific controlled-impedance width.
        # D_ESD sits right on this path (placed there for exactly this
        # reason - see boarddef.py's comment on it), so the pair is routed
        # through its pads, not past them, tapping the live signal.
        dp_j1, dm_j1 = p("J1", "A6"), p("J1", "A7")
        dp_u1, dm_u1 = p("U1", "14"), p("U1", "13")
        dp_esd, dm_esd = p("D_ESD", "3"), p("D_ESD", "1")
        self.track("USB_DP", [dp_j1, (dp_j1[0], dp_esd[1]), dp_esd,
                              (dp_esd[0], dp_u1[1]), dp_u1], TRACK)
        self.track("USB_DM", [dm_j1, (dm_j1[0], dm_esd[1]), dm_esd,
                              (dm_esd[0], dm_u1[1]), dm_u1], TRACK)

        # --- KEY1..KEYn: switch pin 1 -> expander P0x ----------------------
        # Every key (including the PTT key - it's still wired through the
        # expander like any other; firmware is what treats slot 4 as mic
        # rather than an agent key, not the netlist) runs mostly vertical
        # at its own switch's x, jogging onto U2's pin column only at its
        # own target pin's y (each ~0.65mm apart) so horizontal segments
        # stay clear of the next key's vertical run. Keys 1 and 4 (the
        # outermost two) dive to B.Cu for part of the run: key 1's column
        # crosses the LED chain's own head trace (LEDCH0, R7 -> D1) on
        # F.Cu; key 4's sits almost exactly on R6 pin 1's position and
        # its +3V3 B.Cu feed, so it jogs out to x=68 (clear of both)
        # before heading up and back in to its own target pin.
        exp_pin_order = ["4", "5", "6", "7", "8", "9", "10", "11",
                         "13", "14", "15", "16", "17", "18", "19", "20"]
        for i in range(self.n):
            net = "KEY%d" % (i + 1)
            sw_pad = p("SW%d" % (i + 1), "1")
            u2_pad = p("U2", exp_pin_order[i])
            if i == 0:
                self.track(net, [sw_pad, (sw_pad[0], 50.0)], TRACK)
                self.via(net, sw_pad[0], 50.0)
                self.track(net, [(sw_pad[0], 50.0), (sw_pad[0], u2_pad[1])],
                           TRACK, pcbnew.B_Cu)
                self.via(net, sw_pad[0], u2_pad[1])
                self.track(net, [(sw_pad[0], u2_pad[1]), u2_pad], TRACK)
            elif i == self.n - 1:
                # x=80: J2's own horizontal B.Cu runs only span x up to its
                # own pads (76/78.54) on their way to U1 - anything further
                # right than that is clear of all six of them, as well as
                # of R6/U2/D_ESD.
                self.track(net, [sw_pad, (sw_pad[0], 49.0)], TRACK)
                self.via(net, sw_pad[0], 49.0)
                self.track(net, [(sw_pad[0], 49.0), (80.0, 49.0), (80.0, 23.0)],
                           TRACK, pcbnew.B_Cu)
                self.via(net, 80.0, 23.0)
                self.track(net, [(80.0, 23.0), (80.0, u2_pad[1]),
                                 u2_pad], TRACK)
            else:
                self.track(net, [sw_pad, (sw_pad[0], u2_pad[1]), u2_pad], TRACK)

    # -- easter eggs -------------------------------------------------------
    #
    # Scheme "The Tally" - see hardware/kicad/README.md. Three marks, all on
    # the back of the board, all driven by boarddef.REV and the tier name so
    # every tier and every revision is identifiable without a part number.

    def easter_eggs(self):
        W, H = self.W, self.H
        rev = self.meta["rev"]
        base_y = H - 19.0

        # (1) Morse strip: the tier's nickname, drawn as a decorative dashed
        #     rule on the back silkscreen.
        bars = boarddef.morse_bars(self.meta["egg_word"], 8.0, base_y)
        for cx, cy, w, h in bars:
            self.poly(pcbnew.B_SilkS,
                      [(cx - w / 2.0, cy - h / 2.0), (cx + w / 2.0, cy - h / 2.0),
                       (cx + w / 2.0, cy + h / 2.0), (cx - w / 2.0, cy + h / 2.0)])

        # (2) Revision tally: count the strokes, get the revision.
        for a, b in boarddef.tally_marks(rev, 8.0, base_y + 2.6):
            self.seg(pcbnew.B_SilkS, a, b, 0.25)

        # (3) Graffiti tag instead of a sterile REV label.
        self.text(pcbnew.B_SilkS, "~ %s  r%d  %s ~"
                  % (self.meta["nick"].split()[-1].lower(), rev,
                     boarddef.BUILD_TAG),
                  8.0 + 1.0, base_y + 8.2, size=1.4, thick=0.22, angle=6,
                  mirror=True, just=pcbnew.GR_TEXT_H_ALIGN_RIGHT)
        self.seg(pcbnew.B_SilkS, (7.0, base_y + 9.6), (30.0, base_y + 9.0), 0.3)
        self.seg(pcbnew.B_SilkS, (30.0, base_y + 9.0), (34.0, base_y + 10.6), 0.3)

        # (4) The mascot: bare-copper art, made by opening the solder mask over
        #     the back ground pour. On a black board it reads as a bright metal
        #     pictogram you only find when you turn the keypad over.
        ox, oy, k = W - 21.0, H - 20.0, 0.62
        for pts in boarddef.mascot_polys(self.tier):
            self.poly(pcbnew.B_Mask,
                      [(ox + x * k, oy + y * k) for x, y in pts])

    # -- human-readable silkscreen ----------------------------------------

    def silkscreen(self):
        rows, n = self.rows, self.n
        self.text(pcbnew.F_SilkS, self.meta["nick"], self.W / 2.0,
                  boarddef.STRIP - 2.8,
                  size=2.2, thick=0.3,
                  just=pcbnew.GR_TEXT_H_ALIGN_CENTER)
        for i in range(n):
            kx, ky = boarddef.key_center(i, rows)
            label = "PTT" if i == n - 1 else "%d" % (i + 1)
            self.text(pcbnew.F_SilkS, label, kx, ky - 8.9, size=1.1, thick=0.18,
                      just=pcbnew.GR_TEXT_H_ALIGN_CENTER)
        self.text(pcbnew.B_SilkS,
                  "Switchboard %s  %d-key agent status keypad" %
                  (self.meta["nick"], n),
                  self.W / 2.0, 11.0, size=1.6, thick=0.25, mirror=True,
                  just=pcbnew.GR_TEXT_H_ALIGN_CENTER)
        self.text(pcbnew.Cmts_User,
                  "ESP32-S3 PCB antenna keepout - keep copper out of this area",
                  2.0, self.ANT_H - 1.5, size=1.0, thick=0.15,
                  just=pcbnew.GR_TEXT_H_ALIGN_LEFT)

    # -- stackup / appearance ---------------------------------------------

    @staticmethod
    def set_black_stackup(path):
        """Black soldermask / white silkscreen, written straight into the
        stackup block. The pcbnew Python bindings do not expose
        BOARD_STACKUP, so this is done as a post-save edit."""
        txt = open(path).read()
        if "(stackup" not in txt:
            block = (
                '\t\t(stackup\n'
                '\t\t\t(layer "F.SilkS"\n\t\t\t\t(type "Top Silk Screen")\n'
                '\t\t\t\t(color "White")\n\t\t\t)\n'
                '\t\t\t(layer "F.Paste"\n\t\t\t\t(type "Top Solder Paste")\n\t\t\t)\n'
                '\t\t\t(layer "F.Mask"\n\t\t\t\t(type "Top Solder Mask")\n'
                '\t\t\t\t(color "Black")\n\t\t\t\t(thickness 0.01)\n\t\t\t)\n'
                '\t\t\t(layer "F.Cu"\n\t\t\t\t(type "copper")\n\t\t\t\t(thickness 0.035)\n\t\t\t)\n'
                '\t\t\t(layer "dielectric 1"\n\t\t\t\t(type "core")\n\t\t\t\t(thickness 1.51)\n'
                '\t\t\t\t(material "FR4")\n\t\t\t\t(epsilon_r 4.5)\n\t\t\t\t(loss_tangent 0.02)\n\t\t\t)\n'
                '\t\t\t(layer "B.Cu"\n\t\t\t\t(type "copper")\n\t\t\t\t(thickness 0.035)\n\t\t\t)\n'
                '\t\t\t(layer "B.Mask"\n\t\t\t\t(type "Bottom Solder Mask")\n'
                '\t\t\t\t(color "Black")\n\t\t\t\t(thickness 0.01)\n\t\t\t)\n'
                '\t\t\t(layer "B.Paste"\n\t\t\t\t(type "Bottom Solder Paste")\n\t\t\t)\n'
                '\t\t\t(layer "B.SilkS"\n\t\t\t\t(type "Bottom Silk Screen")\n'
                '\t\t\t\t(color "White")\n\t\t\t)\n'
                '\t\t\t(copper_finish "ENIG")\n'
                '\t\t\t(dielectric_constraints no)\n'
                '\t\t)\n')
            txt = txt.replace("\t(setup\n", "\t(setup\n" + block, 1)
        else:
            txt = txt.replace('(color "Green")', '(color "Black")')
        open(path, "w").write(txt)

    def design_rules(self):
        d = self.board.GetDesignSettings()
        d.m_MinThroughDrill = mm(0.15)     # the WROOM-1 thermal vias are 0.2mm
        d.m_MinResolvedSpokes = 1          # hand-solder thermals, not 2-spoke
        d.m_TrackMinWidth = mm(0.2)
        d.m_MinClearance = mm(0.2)

    def fill(self):
        filler = pcbnew.ZONE_FILLER(self.board)
        filler.Fill(self.board.Zones())

    def run(self):
        self.place_parts()
        self.outline()
        self.mounting_holes()
        self.gnd_zones()
        self.stitching_vias()
        self.route_leds()
        self.route_strip()
        # self.route_remaining()  # disabled: most of its hardcoded staging
        # coordinates (CC1/CC2, RESET/BOOT, EXP_INT's main U1->U2 leg, the
        # J2 header, the USB diff pair, KEY1-4) still assume the pre-resize
        # layout and now cross things that didn't exist at those positions
        # before. Only its R1/R4/R5/R6 +3V3 taps and the EXP_INT->R6 hop
        # were fixed for the new layout (see route_strip's I2C section and
        # the comments just above this call in the source) - the rest is
        # handed to FreeRouting instead, same approach that already closed
        # out the D_ESD/J1/J2-GND nets earlier this session.
        self.silkscreen()
        self.easter_eggs()
        self.design_rules()
        self.fill()
        outdir = os.path.join(OUT_ROOT, self.tier)
        path = os.path.join(outdir, self.tier + ".kicad_pcb")
        pcbnew.SaveBoard(path, self.board)
        self.set_black_stackup(path)
        return path


if __name__ == "__main__":
    for tier in ("4key", "8key", "12key"):
        b = BoardBuilder(tier)
        print("wrote %s  (%.1f x %.1f mm)" % (b.run(), b.W, b.H))
