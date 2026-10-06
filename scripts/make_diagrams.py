"""Regenerate Brevet's diagrams (docs/assets/*-light.svg and *-dark.svg).

Usage:  python scripts/make_diagrams.py [output_dir]   (needs Pillow)

The diagrams come in light and dark versions for GitHub's two colour modes.

Every piece of text is measured with Liberation Sans (Arial metrics, which
is what GitHub renders with), wrapped to its box, and checked for overflow,
so the diagrams cannot silently clip. Arrowheads are explicit polygons, not
markers, so every renderer draws them identically.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from PIL import ImageFont

_CANDIDATES = {
    # (bold, mono) -> font files with Arial-compatible metrics, in preference order
    (False, False): ["LiberationSans-Regular.ttf", "Arial.ttf", "arial.ttf"],
    (True, False): ["LiberationSans-Bold.ttf", "Arial Bold.ttf", "arialbd.ttf"],
    (False, True): ["DejaVuSansMono.ttf", "Menlo.ttc", "consola.ttf", "Courier New.ttf"],
}
_SEARCH = [
    os.environ.get("BREVET_DIAGRAM_FONT_DIR", ""),
    "/usr/share/fonts/truetype/liberation", "/usr/share/fonts/truetype/liberation2",
    "/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/liberation",
    "/System/Library/Fonts/Supplemental", "/System/Library/Fonts", "/Library/Fonts",
    "C:/Windows/Fonts",
]
_FONTS: dict = {}


def _find(bold: bool, mono: bool) -> str:
    for name in _CANDIDATES[(bold and not mono, mono)]:
        for d in _SEARCH:
            if d and Path(d, name).exists():
                return str(Path(d, name))
    raise SystemExit("No Arial-compatible font found. Install Liberation Sans or set "
                     "BREVET_DIAGRAM_FONT_DIR to a folder containing Arial.")


def font(size: float, bold: bool = False, mono: bool = False):
    key = (size, bold, mono)
    if key not in _FONTS:
        _FONTS[key] = ImageFont.truetype(_find(bold, mono), round(size))
    return _FONTS[key]


def width(text: str, size: float, bold: bool = False, mono: bool = False) -> float:
    return font(size, bold, mono).getlength(text)


def wrap(text: str, size: float, maxw: float, bold: bool = False) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if width(trial, size, bold) <= maxw:
            cur = trial
        else:
            if not cur:
                raise ValueError(f"word too wide: {w!r}")
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


THEMES = {
    "light": {"bg": "#FFFFFF", "card": "#F5F6F8", "card2": "#FFFFFF", "border": "#D9DDE3",
                  "ink": "#12161B", "body": "#27303A", "muted": "#58626D",
                  "ember": "#D64500", "amber": "#B86A00", "amberfill": "#FFF4E5",
                  "green": "#1C8A4E", "blue": "#2F62B8", "violet": "#6E44B8",
                  "emberfill": "#FDEEE7", "greenfill": "#EAF6EF", "bluefill": "#EDF2FB",
                  "violetfill": "#F2EDFA", "pill": "#E9ECF0", "pilltext": "#2B333C"},
    "dark": {"bg": "#16181B", "card": "#1F2125", "card2": "#24272C", "border": "#383C43",
                 "ink": "#FFFFFF", "body": "#F1F3F6", "muted": "#C3C9D1",
                 "ember": "#FF6A2E", "amber": "#FFAD33", "amberfill": "#2C2416",
                 "green": "#4FD486", "blue": "#93B6F2", "violet": "#BB9AF0",
                 "emberfill": "#2D1D17", "greenfill": "#17271E", "bluefill": "#1A2232",
                 "violetfill": "#231C2F", "pill": "#2C3036", "pilltext": "#E6E9ED"},
}

SANS = "Helvetica, Arial, &apos;Liberation Sans&apos;, sans-serif"
MONO = "Menlo, Consolas, &apos;DejaVu Sans Mono&apos;, monospace"


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class SVG:
    def __init__(self, w: int, h: int, theme: str, title: str):
        self.w, self.h, self.t = w, h, THEMES[theme]
        self.parts: list[str] = []
        self.title = title

    # ---------------------------------------------------------------- shapes
    def rect(self, x, y, w, h, fill, stroke=None, sw=1.5, rx=14, dash=None):
        s = f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}"'
        if stroke:
            s += f' stroke="{stroke}" stroke-width="{sw}"'
        if dash:
            s += f' stroke-dasharray="{dash}"'
        self.parts.append(s + "/>")

    def line(self, x1, y1, x2, y2, color, sw=2.5, dash=None):
        s = (f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" '
             f'stroke-width="{sw}" stroke-linecap="round"')
        if dash:
            s += f' stroke-dasharray="{dash}"'
        self.parts.append(s + "/>")

    def path(self, d, color, sw=2.5, fill="none", dash=None):
        s = (f'<path d="{d}" fill="{fill}" stroke="{color}" stroke-width="{sw}" '
             f'stroke-linecap="round" stroke-linejoin="round"')
        if dash:
            s += f' stroke-dasharray="{dash}"'
        self.parts.append(s + "/>")

    def head(self, x, y, direction, color, size=11):
        """Arrowhead whose tip sits exactly at (x, y)."""
        s = size
        pts = {"right": [(x, y), (x - s * 1.4, y - s * 0.8), (x - s * 1.4, y + s * 0.8)],
               "left": [(x, y), (x + s * 1.4, y - s * 0.8), (x + s * 1.4, y + s * 0.8)],
               "down": [(x, y), (x - s * 0.8, y - s * 1.4), (x + s * 0.8, y - s * 1.4)],
               "up": [(x, y), (x - s * 0.8, y + s * 1.4), (x + s * 0.8, y + s * 1.4)]}[direction]
        p = " ".join(f"{a:.1f},{b:.1f}" for a, b in pts)
        self.parts.append(f'<polygon points="{p}" fill="{color}"/>')

    def arrow(self, x1, y1, x2, y2, color, sw=3, dash=None):
        """Straight horizontal or vertical arrow, tip exactly at (x2, y2)."""
        if y1 == y2:
            d = "right" if x2 > x1 else "left"
            back = 15 if d == "right" else -15
            self.line(x1, y1, x2 - back, y2, color, sw, dash)
        else:
            d = "down" if y2 > y1 else "up"
            back = 15 if d == "down" else -15
            self.line(x1, y1, x2, y2 - back, color, sw, dash)
        self.head(x2, y2, d, color)

    # ------------------------------------------------------------------ text
    def text(self, x, y, s, size, fill, bold=False, anchor="start", mono=False,
             maxw=None, italic=False):
        if maxw is not None and width(s, size, bold, mono) > maxw + 0.5:
            raise ValueError(f"overflow in {self.title!r}: {s!r} "
                             f"({width(s, size, bold, mono):.0f} > {maxw:.0f})")
        fam = MONO if mono else SANS
        wt = ' font-weight="700"' if bold else ""
        it = ' font-style="italic"' if italic else ""
        self.parts.append(f'<text x="{x}" y="{y}" font-family="{fam}" font-size="{size}"'
                          f'{wt}{it} fill="{fill}" text-anchor="{anchor}">{esc(s)}</text>')

    def para(self, x, y, s, size, fill, maxw, lh, bold=False, max_lines=None):
        lines = wrap(s, size, maxw, bold)
        if len(lines) > 1 and len(lines[-1].split()) < 2:
            raise ValueError(f"orphan in {self.title!r}: {s!r} -> {lines}")
        if max_lines and len(lines) > max_lines:
            raise ValueError(f"too many lines in {self.title!r}: {s!r} -> {lines}")
        for i, ln in enumerate(lines):
            self.text(x, y + i * lh, ln, size, fill, bold=bold, maxw=maxw)
        return y + (len(lines) - 1) * lh

    def pill(self, x, y, s, size=15, mono=True, fill=None, color=None, padx=12, h=30):
        w = width(s, size, mono=mono) + 2 * padx
        self.rect(x, y, w, h, fill or self.t["pill"], rx=h / 2)
        self.text(x + padx, y + h / 2 + size * 0.36, s, size, color or self.t["pilltext"],
                  mono=mono)
        return w

    # ----------------------------------------------------------------- icons
    def icon(self, kind, x, y, color, s=1.0):
        g = f'<g transform="translate({x},{y}) scale({s})" fill="none" stroke="{color}" ' \
            f'stroke-width="{2.6 / s:.2f}" stroke-linecap="round" stroke-linejoin="round">'
        body = {
            "agent": '<rect x="7" y="12" width="22" height="17" rx="5"/>'
                     '<line x1="18" y1="5" x2="18" y2="12"/><circle cx="18" cy="4" r="2"/>'
                     f'<circle cx="13.5" cy="20.5" r="2" fill="{color}" stroke="none"/>'
                     f'<circle cx="22.5" cy="20.5" r="2" fill="{color}" stroke="none"/>',
            "person": '<circle cx="18" cy="11" r="6"/><path d="M6 32 C6 22 30 22 30 32"/>',
            "pencil": '<path d="M8 28 L10 21 L24 7 L29 12 L15 26 Z"/><path d="M21 10 L26 15"/>',
            "search": '<circle cx="15" cy="15" r="9"/><path d="M22 22 L30 30"/>'
                      '<path d="M11 15 h8 M15 11 v8"/>',
            "approve": '<circle cx="18" cy="18" r="13"/><path d="M11.5 18.5 L16 23 L25 13"/>',
            "test": '<path d="M6 26 A12 12 0 0 1 30 26"/><path d="M18 26 L25 16"/>'
                    f'<circle cx="18" cy="26" r="2.2" fill="{color}" stroke="none"/>',
            "release": '<path d="M6 12 L18 6 L30 12 L30 26 L18 32 L6 26 Z"/>'
                       '<path d="M6 12 L18 18 L30 12"/><path d="M18 18 L18 32"/>',
            "undo": '<path d="M13 9 L7 15 L13 21"/><path d="M7 15 H22 a8 8 0 0 1 0 16 H14"/>',
            "log": '<rect x="7" y="5" width="22" height="26" rx="4"/>'
                   '<path d="M12 13 h12 M12 19 h12 M12 25 h8"/>',
            "shield": '<path d="M18 4 L30 9 V18 C30 26 24 31 18 33 C12 31 6 26 6 18 V9 Z"/>'
                      '<path d="M12.5 18.5 L16.5 22.5 L24 14"/>',
            "doc": '<path d="M9 5 H22 L28 11 V31 H9 Z"/><path d="M22 5 V11 H28"/>'
                   '<path d="M13 18 h11 M13 24 h11"/>',
            "users": '<circle cx="13" cy="12" r="5"/><path d="M3 30 C3 21 23 21 23 30"/>'
                     '<circle cx="25" cy="11" r="4.5"/><path d="M24 20 C30 20 34 24 34 30"/>',
            "clock": '<circle cx="18" cy="18" r="13"/><path d="M18 10 V18 L24 21"/>',
        }[kind]
        self.parts.append(g + body + "</g>")

    def badge(self, cx, cy, label, color, r=18):
        self.parts.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{color}"/>')
        self.text(cx, cy + 6.5, label, 18, self.t["bg"], bold=True, anchor="middle")

    # ----------------------------------------------------------------- frame
    def frame(self, title, subtitle):
        t = self.t
        self.rect(1, 1, self.w - 2, self.h - 2, t["bg"], t["border"], 1.5, rx=22)
        self.text(56, 72, title, 34, t["ink"], bold=True, maxw=self.w - 112)
        self.para(56, 108, subtitle, 19, t["muted"], self.w - 112, 26, max_lines=2)

    def render(self) -> str:
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.w} {self.h}" '
                f'width="{self.w}" height="{self.h}" role="img" aria-label="{esc(self.title)}">'
                f'<title>{esc(self.title)}</title>' + "".join(self.parts) + "</svg>\n")


# =========================================================================
# 1. How Brevet works (the hero loop)
# =========================================================================
def how_it_works(theme):
    s = SVG(1200, 912, theme, "The governed evolution loop: work, override, dream, dawn, "
                              "evals, release, and recall")
    t = s.t
    s.frame("The governed evolution loop",
            "Experts' corrections become overrides, overrides become candidates, and only a "
            "named human can promote one.")
    W, H, gap, x0 = 340, 236, 46, 48
    xs = [x0, x0 + W + gap, x0 + 2 * (W + gap)]
    y1, y2 = 156, 156 + H + 82
    steps = [
        # (x, y, number, title, icon, color, body, code)
        (xs[0], y1, "1", "Work", "agent", t["green"],
         "The agent drafts its work under one signed harness. An expert checks each draft.",
         "agent.run()"),
        (xs[1], y1, "2", "Override", "pencil", t["blue"],
         "The expert corrects the draft. Brevet records the change and the reason as an override.",
         "agent.record_final()"),
        (xs[2], y1, "3", "Dream", "search", t["violet"],
         "Offline, recurring overrides become candidate capabilities, with no authority yet.",
         "agent.dream()"),
        (xs[2], y2, "4", "Dawn", "approve", t["ember"],
         "A named human or mission group promotes, holds or rejects each candidate.",
         "agent.dawn()"),
        (xs[1], y2, "5", "Evals", "test", t["amber"],
         "Overrides replay as tests. The conservative gate must pass before release.",
         "agent.evaluate()"),
        (xs[0], y2, "6", "Release", "release", t["green"],
         "Promoted capabilities ship in a signed release. capabilities.lock lists each one.",
         "agent.release()"),
    ]
    for x, y, n, title, ic, col, body, code in steps:
        emph = n == "4"
        s.rect(x, y, W, H, t["emberfill"] if emph else t["card"],
               t["ember"] if emph else t["border"], 2.6 if emph else 1.5, rx=16)
        s.badge(x + 38, y + 44, n, col)
        s.text(x + 70, y + 52, title, 25, t["ink"], bold=True, maxw=W - 70 - 64)
        s.icon(ic, x + W - 58, y + 24, col, 1.05)
        s.para(x + 30, y + 104, body, 19.5, t["body"], W - 60, 28, max_lines=3)
        s.pill(x + 30, y + H - 50, code, 15)

    my1 = y1 + H / 2
    my2 = y2 + H / 2
    s.arrow(xs[0] + W + 6, my1, xs[1] - 6, my1, t["amber"])
    s.arrow(xs[1] + W + 6, my1, xs[2] - 6, my1, t["amber"])
    cx3 = xs[2] + W / 2
    s.arrow(cx3, y1 + H + 6, cx3, y2 - 6, t["amber"])
    s.text(cx3 - 16, y1 + H + 48, "candidates, no authority yet", 16, t["muted"], anchor="end",
           maxw=300)
    s.arrow(xs[2] - 6, my2, xs[1] + W + 6, my2, t["amber"])
    s.arrow(xs[1] - 6, my2, xs[0] + W + 6, my2, t["amber"])
    cx1 = xs[0] + W / 2
    s.arrow(cx1, y2 - 6, cx1, y1 + H + 6, t["green"])
    s.text(cx1 + 16, y1 + H + 48, "the next version starts work", 16, t["muted"], maxw=300)

    by = y2 + H + 58
    s.line(cx1, y2 + H + 6, cx1, by - 15, t["ember"], 2.5, dash="6 6")
    s.head(cx1, by - 4, "down", t["ember"], 9)
    bw = xs[2] + W - x0
    s.rect(x0, by, bw, 92, t["emberfill"], t["ember"], 2, rx=16, dash="8 6")
    s.icon("undo", x0 + 28, by + 28, t["ember"], 1.0)
    s.text(x0 + 82, by + 40, "Recall", 23, t["ember"], bold=True)
    s.text(x0 + 82, by + 70, "A capability that proves wrong is recalled, and every release "
           "that shipped it is flagged.", 19, t["body"], maxw=bw - 82 - 210)
    pw = width("agent.recall()", 15, mono=True) + 24
    s.pill(x0 + bw - pw - 28, by + 31, "agent.recall()", 15)

    s.text(600, s.h - 26, "Every step appends an envelope to a hash-linked evidence chain, so "
           "later edits show up on replay.", 16.5, t["muted"], anchor="middle", maxw=1100)
    return s.render()


# =========================================================================
# 2. The worked example as a timeline
# =========================================================================
def example(theme):
    s = SVG(1200, 760, theme, "Worked example: one capability from repeated override to "
                              "dream, dawn, release, and recall")
    t = s.t
    s.frame("One capability, from override to recall",
            "A quality reviewer at a pharmaceutical plant checks an agent's severity rating "
            "for each equipment problem.")
    rows = [
        ("WEEKS 1 TO 3: WORK AND OVERRIDE", "The reviewer keeps overriding the same call",
         "pencil", t["blue"], t["bluefill"],
         ("The agent rates pump vibration during cleaning as minor. The reviewer changes it "
          "to major each time: it is an early sign of seal wear."),
         "4 overrides, each with a reason"),
        ("THAT NIGHT: DREAM", "Brevet proposes a candidate rule", "search", t["violet"],
         t["violetfill"],
         ("The dream cycle finds the same override four times and drafts: vibration during "
          "cleaning means major. The candidate has no authority yet."),
         "1 candidate, Evidence layer"),
        ("NEXT MORNING: DAWN", "The mission group promotes it", "approve", t["green"],
         t["greenfill"],
         ("Release 0.2.0 ships. capabilities.lock names the rule, the overrides behind it and "
          "who approved it."),
         "release 0.2.0, signed"),
        ("MONTHS LATER: RECALL", "The rule is recalled", "undo", t["ember"], t["emberfill"],
         ("Engineers trace the vibration to a faulty sensor. The mission group recalls the "
          "rule, and Brevet flags release 0.2.0."),
         "recalled, 0.2.0 flagged"),
    ]
    top, step, lx = 170, 138, 104
    s.line(lx, top + 30, lx, top + 3 * step + 30, t["border"], 3)
    for i, (when, head, ic, col, fill, body, chip) in enumerate(rows):
        y = top + i * step
        s.parts.append(f'<circle cx="{lx}" cy="{y + 30}" r="27" fill="{fill}" '
                       f'stroke="{col}" stroke-width="2.5"/>')
        s.icon(ic, lx - 17, y + 13, col, 0.95)
        s.text(156, y + 22, when, 15, col, bold=True, maxw=300)
        s.text(156, y + 52, head, 23, t["ink"], bold=True, maxw=680)
        s.para(156, y + 82, body, 18.5, t["body"], 640, 26, max_lines=3)
        cw = width(chip, 16, bold=True) + 32
        cx = 1200 - 56 - cw
        s.rect(cx, y + 34, cw, 38, fill, col, 1.8, rx=19)
        s.text(cx + cw / 2, y + 59, chip, 16, col, bold=True, anchor="middle")
    s.text(600, s.h - 26, "The same lifecycle applies to every kind of capability: prompt "
           "rules, skills, tool bindings and eval cases.", 16.5, t["muted"], anchor="middle",
           maxw=1100)
    return s.render()


# =========================================================================
# 3. Authority levels
# =========================================================================
def levels(theme):
    s = SVG(1200, 600, theme, "The authority ladder: Evidence, Advisory, Controlled, "
                              "and Recalled")
    t = s.t
    s.frame("The authority ladder",
            "Every capability enters at Evidence. Only a recorded human decision raises its "
            "authority.")
    W, H, gap, x0, y = 284, 236, 138, 40, 158
    cards = [
        ("Evidence", "Candidates only", t["blue"], t["bluefill"],
         "No operational authority. Humans can inspect it; evals can test it."),
        ("Advisory", "Shapes drafts", t["amber"], t["amberfill"],
         "May inform what the agent drafts. A human still checks each result."),
        ("Controlled", "Drives actions", t["green"], t["greenfill"],
         "May drive actions directly, with no human checking each result."),
    ]
    xs = [x0 + i * (W + gap) for i in range(3)]
    for x, (name, tag, col, fill, body) in zip(xs, cards):
        s.rect(x, y, W, H, fill, col, 2.4, rx=16)
        s.text(x + 26, y + 48, name, 26, col, bold=True, maxw=W - 52)
        s.text(x + 26, y + 80, tag, 19, t["ink"], bold=True, maxw=W - 52)
        s.para(x + 26, y + 118, body, 18.5, t["body"], W - 52, 27, max_lines=4)
    labels = [("promoted at the", "dawn gate"), ("promoted by the", "mission group")]
    for i, (a, b) in enumerate(labels):
        xa, xb = xs[i] + W + 8, xs[i + 1] - 8
        my = y + H / 2 + 14
        s.arrow(xa, my, xb, my, t["ember"], 3.2)
        mid = (xa + xb) / 2
        s.text(mid, my - 38, a, 15, t["muted"], anchor="middle", maxw=gap - 6)
        s.text(mid, my - 17, b, 15.5, t["ink"], bold=True, anchor="middle", maxw=gap - 6)

    wx, wy, ww, wh = 280, 452, 640, 84
    s.rect(wx, wy, ww, wh, t["emberfill"], t["ember"], 2, rx=16, dash="8 6")
    s.icon("undo", wx + 24, wy + 24, t["ember"], 1.0)
    s.text(wx + 76, wy + 37, "Recalled", 21, t["ember"], bold=True)
    s.text(wx + 76, wy + 64, "Leaves future releases; every release that shipped it is flagged.",
           17, t["body"], maxw=ww - 96)
    ax = xs[1] + W / 2
    s.line(ax, y + H + 6, ax, wy - 15, t["ember"], 2.4, dash="6 6")
    s.head(ax, wy - 4, "down", t["ember"], 9)
    cx = xs[2] + W / 2
    s.path(f"M{cx} {y + H + 6} V{wy + wh / 2 - 10} Q{cx} {wy + wh / 2} {cx - 10} "
           f"{wy + wh / 2} H{wx + ww + 15}", t["ember"], 2.4, dash="6 6")
    s.head(wx + ww + 4, wy + wh / 2, "left", t["ember"], 9)
    s.text(600, s.h - 26, "Approver identities in the agent:, model: and dream: namespaces are "
           "rejected.", 17, t["muted"], anchor="middle", maxw=1100)
    return s.render()


# =========================================================================
# 4. Wrapping an existing agent
# =========================================================================
def wrap_diagram(theme):
    s = SVG(1200, 640, theme, "Brevet wraps the agent you already have and keeps the "
                              "records around it")
    t = s.t
    s.frame("Brevet wraps the agent you already have",
            "Your framework keeps running the agent. Brevet keeps the manifest, the evidence "
            "and the releases around it.")
    fx, fy, fw, fh = 300, 166, 860, 392
    s.rect(fx, fy, fw, fh, t["card"], t["ember"], 2.6, rx=24)
    ix, iy, iw, ih = 420, 262, 620, 200
    s.rect(ix, iy, iw, ih, t["card2"], t["border"], 1.8, rx=18)
    s.icon("agent", ix + iw / 2 - 18, iy + 22, t["ink"], 1.0)
    s.text(ix + iw / 2, iy + 92, "Your agent, unchanged", 26, t["ink"], bold=True,
           anchor="middle", maxw=iw - 40)
    chips = ["LangGraph", "CrewAI", "AutoGen", "Claude Agent SDK", "and more"]
    widths = [width(c, 15.5, bold=True) + 26 for c in chips]
    total = sum(widths) + 8 * (len(chips) - 1)
    if total > iw - 30:
        raise ValueError(f"chips overflow: {total}")
    cx = ix + (iw - total) / 2
    for c, w in zip(chips, widths):
        last = c == "and more"
        s.rect(cx, iy + 116, w, 32, t["amber"] if last else t["pill"], rx=16)
        s.text(cx + w / 2, iy + 137, c, 15.5, t["bg"] if last else t["pilltext"], bold=True,
               anchor="middle")
        cx += w + 8
    s.text(ix + iw / 2, iy + 178, "or any Python function", 16, t["muted"], anchor="middle")

    tabs = [
        (320, fy - 38, "doc", "Signed manifest", "the harness as a signed file"),
        (820, fy - 38, "log", "Evidence chain", "every step, hash-linked"),
        (320, fy + fh - 38, "users", "Dawn gate", "named humans promote"),
        (820, fy + fh - 38, "release", "capabilities.lock", "what it knows, who approved it"),
    ]
    for x, y, ic, head, sub in tabs:
        tw, th = 320, 76
        s.rect(x, y, tw, th, t["card2"], t["amber"], 2, rx=14)
        s.icon(ic, x + 16, y + 20, t["amber"], 1.0)
        s.text(x + 64, y + 33, head, 20, t["ink"], bold=True, maxw=tw - 78)
        s.text(x + 64, y + 58, sub, 15.5, t["muted"], maxw=tw - 78)

    pw = width("brevet.wrap(agent)", 17, mono=True) + 32
    py = fy + fh / 2 - 26
    s.rect(40, py, pw, 52, t["card2"], t["amber"], 2, rx=14)
    s.text(40 + pw / 2, py + 32, "brevet.wrap(agent)", 17, t["amber"], mono=True,
           anchor="middle")
    s.arrow(40 + pw + 8, py + 26, fx - 6, py + 26, t["amber"])
    return s.render()


# =========================================================================
# 5. Where Brevet fits
# =========================================================================
def suite(theme):
    s = SVG(1200, 520, theme, "Where Brevet fits: CHAP, Metis, and Brevet each answer one "
                              "question")
    t = s.t
    s.frame("Where Brevet fits",
            "Three Brightbeam projects, each answering one question about a working agent.")
    W, gap, x0, y, H = 350, 35, 40, 156, 236
    cards = [
        ("CHAP", "What happened?", "log", t["blue"], t["bluefill"],
         "Records the work and every review decision, in a log anyone can check."),
        ("Metis", "What do our experts know?", "users", t["green"], t["greenfill"],
         "Captures experts' know-how as memory an agent may use, only where it holds."),
        ("Brevet", "How does the agent change?", "shield", t["amber"], t["amberfill"],
         "Governs what the agent learns: promotion at the dawn gate, signed releases and recall."),
    ]
    xs = [x0 + i * (W + gap) for i in range(3)]
    for i, (x, (name, q, ic, col, fill, body)) in enumerate(zip(xs, cards)):
        emph = i == 2
        s.rect(x, y, W, H, fill, col, 3 if emph else 2, rx=16)
        s.icon(ic, x + 26, y + 26, col, 1.05)
        s.text(x + 78, y + 54, name, 27, col, bold=True, maxw=W - 100)
        s.text(x + 26, y + 104, q, 21, t["ink"], bold=True, maxw=W - 52)
        s.para(x + 26, y + 142, body, 18.5, t["body"], W - 52, 27, max_lines=3)
    cy = y + H
    a, b = xs[0] + W / 2, xs[2] + W / 2
    s.path(f"M{a} {cy + 6} V{cy + 38} Q{a} {cy + 48} {a + 10} {cy + 48} H{b - 10} "
           f"Q{b} {cy + 48} {b} {cy + 38} V{cy + 21}", t["blue"], 2.6)
    s.head(b, cy + 6, "up", t["blue"], 9)
    s.text(600, cy + 80, "CHAP's review decisions can flow into Brevet as evidence.", 17,
           t["muted"], anchor="middle", maxw=1000)
    return s.render()


def main(outdir: str):
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    made = []
    for name, fn in [("how-it-works", how_it_works), ("example", example),
                     ("levels", levels), ("wrap", wrap_diagram), ("suite", suite)]:
        for theme in ("light", "dark"):
            p = out / f"{name}-{theme}.svg"
            p.write_text(fn(theme), encoding="utf-8")
            made.append(p.name)
    print("wrote:", ", ".join(made))


if __name__ == "__main__":
    args = sys.argv[1:]
    if any(a.startswith("-") for a in args):
        print("usage: python scripts/make_diagrams.py [output-folder]\n"
              "With no folder, writes docs/assets/ and refreshes the copies "
              "embedded in docs/demo.html.")
        sys.exit(0)
    default = Path(__file__).resolve().parent.parent / "docs" / "assets"
    main(args[0] if args else str(default))
    if not args:
        from embed_demo_assets import main as embed_demo  # sibling script
        embed_demo()
