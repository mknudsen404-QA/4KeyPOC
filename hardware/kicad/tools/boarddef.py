"""Shared parametric definition of the Switchboard agent-status keypad line.

One definition drives BOTH generators so the schematic and the PCB can never
drift apart:

    gen_sch.py   -> <tier>.kicad_sch / .kicad_pro   (plain python3, hand-written s-expr)
    gen_pcb.py   -> <tier>.kicad_pcb                (KiCad's bundled python + pcbnew API)

Keep this file Python 3.9 compatible: KiCad 10 ships Python 3.9 and gen_pcb.py
has to run under it.

Product line (see docs/design/4key-product-line-idea.md):

    4key  "THE STICK"   1 row  x 4 keys   3 agent slots + push-to-talk
    8key  "THE SWITCH"  2 rows x 4 keys   7 agent slots + push-to-talk
    12key "THE CARROT"  3 rows x 4 keys  11 agent slots + push-to-talk

Product-line rule: the bottom-right key is ALWAYS push-to-talk. Every other
key is an agent slot. That keeps the firmware generalisation trivial
(AGENT_KEY_COUNT = n-1, PTT_KEY_INDEX = n-1) and means muscle memory for the
mic key survives a tier upgrade.
"""

# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

PITCH = 19.05          # standard MX key spacing, 0.75"
COLS = 4               # every tier is 4 columns wide
MARGIN = 5.5           # board margin around the key field
STRIP = 46.0           # electronics strip along the top edge (holds the module,
                       # USB-C, expander, and the ESP32 antenna keepout)
BOARD_W = 2 * MARGIN + COLS * PITCH        # 87.2 mm on every tier

REV = 1                # board revision. BUMP THIS and the tally marks follow.
BUILD_TAG = "2026-09"  # yyyy-mm stamped into the graffiti tag

TIERS = {
    "4key":  {"keys": 4,  "rows": 1, "nick": "THE STICK",  "egg_word": "STICK"},
    "8key":  {"keys": 8,  "rows": 2, "nick": "THE SWITCH", "egg_word": "SWITCH"},
    "12key": {"keys": 12, "rows": 3, "nick": "THE CARROT", "egg_word": "CARROT"},
}


def board_h(rows):
    return STRIP + rows * PITCH + MARGIN


def key_center(idx, rows):
    """Local board coords (mm) of key idx (0-based), row-major, top-left first."""
    col = idx % COLS
    row = idx // COLS
    return (MARGIN + col * PITCH + PITCH / 2.0,
            STRIP + row * PITCH + PITCH / 2.0)


# ---------------------------------------------------------------------------
# Part table
# ---------------------------------------------------------------------------

class Part(object):
    def __init__(self, ref, lib_id, value, footprint, pins,
                 sch=(0.0, 0.0), pcb=(0.0, 0.0), rot=0, nc=(), desc=""):
        self.ref = ref
        self.lib_id = lib_id          # "Device:R"
        self.value = value
        self.footprint = footprint    # "Device_R:R_0603_..." or None
        self.pins = dict(pins)        # {"1": "GND", ...}
        self.sch = sch                # schematic placement (mm)
        self.pcb = pcb                # board-local placement (mm)
        self.rot = rot                # board rotation (deg)
        self.nc = list(nc)            # pin numbers with an explicit no-connect
        self.desc = desc


R0603 = "Resistor_SMD:R_0603_1608Metric_Pad0.98x0.95mm_HandSolder"
C0603 = "Capacitor_SMD:C_0603_1608Metric_Pad1.08x0.95mm_HandSolder"
C0805 = "Capacitor_SMD:C_0805_2012Metric_Pad1.18x1.45mm_HandSolder"


def build(tier):
    """Return (parts, nets_in_order, meta) for one tier."""
    cfg = TIERS[tier]
    n = cfg["keys"]
    rows = cfg["rows"]
    H = board_h(rows)
    parts = []

    # --- U1  ESP32-S3-WROOM-1 module -------------------------------------
    # Antenna end points at the TOP board edge; a copper keepout sits under it.
    esp_pins = {
        "1": "GND", "40": "GND", "41": "GND",
        "2": "+3V3",
        "3": "~RESET",
        "4": "EXP_INT",      # IO4  <- expander /INT
        "12": "SCL",         # IO8  (matches firmware SCL_PIN 8)
        "17": "SDA",         # IO9  (matches firmware SDA_PIN 9)
        "13": "USB_DM",      # USB_D-
        "14": "USB_DP",      # USB_D+
        "5": "LED_GPIO",     # IO5 -> level-shifter input (left side of the module,
                             # which is what lets the LED chain reach the key
                             # field without crossing the I2C lanes)
        "27": "BOOT",        # IO0  strapping / boot button
        "6": "HDR_IO6", "7": "HDR_IO7", "18": "HDR_IO10", "31": "HDR_IO38",
        "36": "HDR_RXD0", "37": "HDR_TXD0",
    }
    # Everything else is deliberately unused. IO35/36/37 are consumed by the
    # octal PSRAM on the N16R8 variant, so they must NOT be broken out; the
    # rest are just spare.
    esp_nc = ["8", "9", "10", "11", "15", "16", "19", "20", "21", "22", "23",
              "24", "25", "26", "28", "29", "30", "32", "33", "34", "35",
              "38", "39"]
    parts.append(Part(
        "U1", "RF_Module:ESP32-S3-WROOM-1", "ESP32-S3-WROOM-1-N16R8",
        "RF_Module:ESP32-S3-WROOM-1", esp_pins,
        sch=(160, 130), pcb=(24.0, 27.9), rot=0, nc=esp_nc,
        desc="Controller. Native USB, I2C master, WS2812 data out."))

    # --- U2  I2C GPIO expander -------------------------------------------
    # XL9555 (the part the firmware already knows) in TSSOP-24 is pin-for-pin
    # the PCF8575 layout; the PCF8575DBR symbol is used as the stand-in.
    exp_pins = {
        "1": "EXP_INT", "2": "GND", "3": "GND", "21": "GND",
        "22": "SCL", "23": "SDA", "24": "+3V3", "12": "GND",
    }
    exp_pin_order = ["4", "5", "6", "7", "8", "9", "10", "11",
                     "13", "14", "15", "16", "17", "18", "19", "20"]
    exp_nc = []
    for i, pin in enumerate(exp_pin_order):
        if i < n:
            exp_pins[pin] = "KEY%d" % (i + 1)
        else:
            exp_nc.append(pin)
    parts.append(Part(
        "U2", "Interface_Expansion:PCF8575DBR", "XL9555 / PCF8575 (TSSOP-24)",
        "Package_SO:TSSOP-24_4.4x7.8mm_P0.65mm", exp_pins,
        sch=(285, 130), pcb=(62.0, 32.0), rot=0, nc=exp_nc,
        desc="16-bit I2C GPIO expander reading every key. A2:A1:A0 = 000 -> 0x20."))

    # --- U3  3V3 LDO ------------------------------------------------------
    parts.append(Part(
        "U3", "Regulator_Linear:AP2112K-3.3", "AP2112K-3.3",
        "Package_TO_SOT_SMD:SOT-23-5",
        {"1": "+5V", "2": "GND", "3": "+5V", "5": "+3V3"},
        sch=(60, 45), pcb=(56.0, 14.0), rot=0, nc=["4"],
        desc="5V VBUS -> 3V3 for the module and the expander."))

    # --- U4  5V level shifter for the LED chain ---------------------------
    parts.append(Part(
        "U4", "74xGxx:74LVC1G17", "74LVC1G17",
        "Package_TO_SOT_SMD:SOT-23-5",
        {"2": "LED_GPIO", "3": "GND", "4": "LED_BUF", "5": "+5V"},
        sch=(160, 255), pcb=(4.5, 30.0), rot=0, nc=["1"],
        desc="Schmitt buffer running off 5V so the WS2812-style chain sees a "
             "legal VIH instead of the ESP32's 3.3V."))

    # --- J1  USB-C --------------------------------------------------------
    usb_pins = {
        "A1": "GND", "B1": "GND", "A12": "GND", "B12": "GND", "SH": "GND",
        "A4": "+5V", "B4": "+5V", "A9": "+5V", "B9": "+5V",
        "A5": "CC1", "B5": "CC2",
        "A6": "USB_DP", "B6": "USB_DP",
        "A7": "USB_DM", "B7": "USB_DM",
    }
    parts.append(Part(
        "J1", "Connector:USB_C_Receptacle_USB2.0_16P", "USB_C_Receptacle",
        "Connector_USB:USB_C_Receptacle_HCTL_HC-TYPE-C-16P-01A", usb_pins,
        sch=(55, 130), pcb=(70.0, 3.0), rot=180, nc=["A8", "B8"],
        desc="Power + native USB serial. Sink only, 5.1k CC pulldowns."))

    # --- J2  spare-GPIO expansion header ----------------------------------
    parts.append(Part(
        "J2", "Connector_Generic:Conn_02x06_Odd_Even", "SPARE / DEBUG",
        "Connector_PinHeader_2.54mm:PinHeader_2x06_P2.54mm_Vertical",
        {"1": "+3V3", "2": "+5V", "3": "GND", "4": "GND",
         "5": "SDA", "6": "SCL",
         "7": "HDR_IO38", "8": "HDR_IO6", "9": "HDR_IO7", "10": "HDR_IO10",
         "11": "HDR_TXD0", "12": "HDR_RXD0"},
        sch=(285, 255), pcb=(76.0, 24.0), rot=0,
        desc="Spare I2C + GPIO for a future encoder / display / second expander."))

    # --- Passives ---------------------------------------------------------
    passives = [
        ("R1", "10k",  R0603, {"1": "+3V3", "2": "~RESET"}, (40, 200), (46.0, 24.0)),
        ("R2", "5.1k", R0603, {"1": "CC1", "2": "GND"},     (60, 200), (66.0, 12.0)),
        ("R3", "5.1k", R0603, {"1": "CC2", "2": "GND"},     (80, 200), (62.5, 12.0)),
        ("R4", "4.7k", R0603, {"1": "+3V3", "2": "SDA"},    (100, 200), (46.0, 28.0)),
        ("R5", "4.7k", R0603, {"1": "+3V3", "2": "SCL"},    (120, 200), (46.0, 32.0)),
        ("R6", "10k",  R0603, {"1": "+3V3", "2": "EXP_INT"}, (140, 200), (66.0, 38.0)),
        ("R7", "330",  R0603, {"1": "LED_BUF", "2": "LEDCH0"}, (160, 200), (9.5, 34.5)),
    ]
    caps = [
        ("C1", "10u",   C0805, {"1": "+5V", "2": "GND"},  (40, 300), (52.0, 19.0)),
        ("C2", "1u",    C0603, {"1": "+5V", "2": "GND"},  (60, 300), (60.0, 10.0)),
        ("C3", "10u",   C0805, {"1": "+3V3", "2": "GND"}, (80, 300), (60.0, 19.0)),
        ("C4", "100n",  C0603, {"1": "+3V3", "2": "GND"}, (100, 300), (11.0, 22.5)),
        ("C5", "100n",  C0603, {"1": "+3V3", "2": "GND"}, (120, 300), (70.0, 24.0), 180),
        ("C6", "1u",    C0603, {"1": "~RESET", "2": "GND"}, (140, 300), (6.0, 36.0)),
        ("C7", "100n",  C0603, {"1": "+5V", "2": "GND"},  (160, 300), (9.5, 30.0)),
        ("C8", "22u",   C0805, {"1": "+5V", "2": "GND"},  (180, 300), (79.0, 16.0)),
    ]
    for row in passives:
        ref, val, fp, pins, sch, pcb = row[:6]
        rot = row[6] if len(row) > 6 else 0
        parts.append(Part(ref, "Device:R", val, fp, pins, sch=sch, pcb=pcb, rot=rot))
    for row in caps:
        ref, val, fp, pins, sch, pcb = row[:6]
        rot = row[6] if len(row) > 6 else 0
        parts.append(Part(ref, "Device:C", val, fp, pins, sch=sch, pcb=pcb, rot=rot))

    # Reset + boot buttons
    parts.append(Part("SW_RST", "Switch:SW_Push", "RESET",
                      "Button_Switch_SMD:SW_Push_1P1T_NO_CK_KMR2",
                      {"1": "~RESET", "2": "GND"}, sch=(60, 355), pcb=(5.0, 40.0)))
    parts.append(Part("SW_BOOT", "Switch:SW_Push", "BOOT",
                      "Button_Switch_SMD:SW_Push_1P1T_NO_CK_KMR2",
                      {"1": "BOOT", "2": "GND"}, sch=(110, 355), pcb=(5.0, 24.0)))

    # --- Key switches, per-key RGB, per-LED decoupling --------------------
    for i in range(n):
        kx, ky = key_center(i, rows)
        sch_x = 360 + (i % COLS) * 40
        sch_y = 60 + (i // COLS) * 35
        parts.append(Part(
            "SW%d" % (i + 1), "Switch:SW_Push",
            "PTT" if i == n - 1 else "AGENT%d" % (i + 1),
            "switchboard:SW_Hotswap_Kailh_MX",
            {"1": "KEY%d" % (i + 1), "2": "GND"},
            sch=(sch_x, sch_y), pcb=(kx, ky)))
        parts.append(Part(
            "D%d" % (i + 1), "LED:SK6812MINI", "SK6812MINI",
            "LED_SMD:LED_SK6812MINI_PLCC4_3.5x3.5mm_P1.75mm",
            # SK6812MINI PLCC4 pinout: 1=DOUT, 2=VSS, 3=DIN, 4=VDD
            {"1": "LEDCH%d" % (i + 1), "2": "GND", "3": "LEDCH%d" % i, "4": "+5V"},
            sch=(sch_x + 160, sch_y), pcb=(kx, ky + 5.0), rot=180,
            nc=["1"] if i == n - 1 else []))
        parts.append(Part(
            "CL%d" % (i + 1), "Device:C", "100n", C0603,
            {"1": "+5V", "2": "GND"},
            sch=(sch_x + 180, sch_y + 20), pcb=(kx - 7.0, ky + 7.6)))

    meta = {
        "tier": tier, "keys": n, "rows": rows, "w": BOARD_W, "h": H,
        "nick": cfg["nick"], "egg_word": cfg["egg_word"], "rev": REV,
    }
    return parts, meta


def net_list(parts):
    """Ordered, de-duplicated list of net names used by a board."""
    seen = []
    for p in parts:
        for net in p.pins.values():
            if net not in seen:
                seen.append(net)
    return seen


# ---------------------------------------------------------------------------
# Easter-egg scheme  ("The Tally")  - see hardware/kicad/README.md
# ---------------------------------------------------------------------------

MORSE = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.",
    "G": "--.", "H": "....", "I": "..", "J": ".---", "K": "-.-", "L": ".-..",
    "M": "--", "N": "-.", "O": "---", "P": ".--.", "Q": "--.-", "R": ".-.",
    "S": "...", "T": "-", "U": "..-", "V": "...-", "W": ".--", "X": "-..-",
    "Y": "-.--", "Z": "--..",
}


def morse_bars(word, x0, y, unit=0.55, gap=0.55, letter_gap=1.65):
    """Yield (x, y, w, h) silkscreen bars spelling `word` in Morse.

    Reads as a decorative dashed rule until somebody notices the rhythm.
    Returns bar rectangles as centre-x, centre-y, width, height.
    """
    out = []
    x = x0
    for li, ch in enumerate(word.upper()):
        code = MORSE.get(ch, "")
        for si, sym in enumerate(code):
            w = unit if sym == "." else unit * 3.0
            out.append((x + w / 2.0, y, w, unit))
            x += w + gap
        x += letter_gap - gap
    return out


def tally_marks(rev, x0, y, h=3.0, pitch=1.1):
    """Yield line segments ((x1,y1),(x2,y2)) drawing `rev` tally strokes.

    Revision 1 = one stroke, revision 2 = two, ... every fifth stroke is the
    diagonal that crosses the preceding four, exactly like a real tally. A
    keyboard nerd holding two boards can count the strokes and know which
    revision is which without reading a single character.
    """
    segs = []
    for i in range(rev):
        group, within = divmod(i, 5)
        gx = x0 + group * (pitch * 5.2)
        if within == 4:
            segs.append(((gx - 0.4, y + h), (gx + pitch * 3 + 0.4, y)))
        else:
            x = gx + within * pitch
            segs.append(((x, y), (x, y + h)))
    return segs


def mascot_polys(tier):
    """The carrot-and-stick mascot, as solder-mask openings on B.Mask.

    One continuing scene across the product line - the reward gets closer as
    you buy more keys:
        4key  THE STICK  - just the stick
        8key  THE SWITCH - stick plus the dangling string
        12key THE CARROT - stick, string, and the carrot finally on the end
    Returned as a list of closed polygons in local mm, origin at the mascot's
    top-left anchor.
    """
    polys = []
    # The stick: a tapered diagonal baton, present on every tier.
    polys.append([(0.0, 0.0), (1.1, 0.35), (12.5, 8.4), (11.6, 9.2)])
    # Grip wrap near the held end.
    polys.append([(2.2, 1.3), (3.0, 1.55), (3.9, 2.5), (3.1, 2.8)])
    if tier in ("8key", "12key"):
        # The string, drawn as a slack catenary-ish ribbon hanging off the tip.
        polys.append([(12.5, 8.4), (12.9, 8.2), (14.6, 12.0), (15.9, 15.2),
                      (15.3, 15.4), (13.9, 12.2)])
    if tier == "12key":
        # The carrot itself: body + three fronds.
        polys.append([(14.2, 16.0), (17.4, 16.4), (16.4, 23.6), (15.2, 23.4)])
        polys.append([(14.6, 15.9), (15.4, 13.6), (16.1, 13.7), (15.6, 15.9)])
        polys.append([(15.7, 15.9), (17.2, 14.0), (17.8, 14.4), (16.6, 16.0)])
        polys.append([(16.7, 16.1), (18.9, 15.2), (19.2, 15.8), (17.4, 16.4)])
    return polys
