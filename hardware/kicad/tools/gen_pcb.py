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

    def via(self, net, x, y):
        v = pcbnew.PCB_VIA(self.board)
        v.SetPosition(P(x, y))
        v.SetViaType(pcbnew.VIATYPE_THROUGH)
        v.SetWidth(mm(VIA_D))
        v.SetDrill(mm(VIA_DRILL))
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
        # upper-left hole sits at x=49 instead. Revisit with the enclosure.
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
        self.track("+3V3", [u3_vout, (73.0, u3_vout[1]), (73.0, u2_vcc[1]),
                            u2_vcc], TRACK)
        self.track("+3V3", [c3, (c3[0], u3_vout[1])], TRACK)
        self.track("+3V3", [(73.0, c5[1]), c5], TRACK)
        # 3V3 crosses to the left-hand region on the back layer.
        u1_3v3, c4 = p("U1", "2"), p("C4", "1")
        self.via("+3V3", 73.0, 25.5)
        self.via("+3V3", 13.0, 25.5)
        self.track("+3V3", [(73.0, 25.5), (13.0, 25.5)], TRACK, pcbnew.B_Cu)
        self.track("+3V3", [(13.0, 25.5), (13.0, u1_3v3[1]), u1_3v3], TRACK)
        self.track("+3V3", [(13.0, u1_3v3[1]), (c4[0], u1_3v3[1]), c4], TRACK)

        # --- I2C, on the back layer ----------------------------------------
        scl_u1, sda_u1 = p("U1", "12"), p("U1", "17")
        scl_u2, sda_u2 = p("U2", "22"), p("U2", "23")
        self.track("SCL", [scl_u1, (13.5, scl_u1[1])], TRACK)
        self.via("SCL", 13.5, scl_u1[1])
        self.track("SCL", [(13.5, scl_u1[1]), (13.5, 33.5), (67.0, 33.5)],
                   TRACK, pcbnew.B_Cu)
        self.via("SCL", 67.0, 33.5)
        self.track("SCL", [(67.0, 33.5), (67.0, scl_u2[1]), scl_u2], TRACK)

        self.track("SDA", [sda_u1, (sda_u1[0], 42.0)], TRACK)
        self.via("SDA", sda_u1[0], 42.0)
        self.track("SDA", [(sda_u1[0], 42.0), (sda_u1[0], 38.0), (69.0, 38.0)],
                   TRACK, pcbnew.B_Cu)
        self.via("SDA", 69.0, 38.0)
        self.track("SDA", [(69.0, 38.0), (69.0, sda_u2[1]), sda_u2], TRACK)

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
        self.text(pcbnew.F_SilkS, self.meta["nick"], self.W / 2.0, 43.2,
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
