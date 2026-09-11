"""Render the Telegram and social artwork from the same mark the site uses.

The geometry is `site/src/components/Logo.astro` verbatim — a 48x48 instrument frame, three range
rings struck from the bottom-left corner, the sweep, and one blip where the sweep crosses the middle
ring. Nothing here is drawn by eye; every coordinate is that file's, multiplied by a scale.

The palette and type are `site/src/styles/tokens.css`: pitch black, structure from hairlines, colour
only where it carries meaning, Space Grotesk for the wordmark and JetBrains Mono for the readouts.

Run:  .venv/Scripts/python assets/brand/make_brand.py
Fonts are cached in assets/brand/.fonts (gitignored) and fetched once from the Google Fonts upstream.
"""
from __future__ import annotations

import math
import pathlib
import urllib.request

from PIL import Image, ImageDraw, ImageFont

HERE = pathlib.Path(__file__).parent
FONTS = HERE / ".fonts"
SS = 3  # supersample; hairlines survive the downscale, jaggies do not

# ---------------------------------------------------------------- palette (tokens.css)
BLACK = "#000000"
WHITE = "#ffffff"
CARBON = "#606060"
GRAPHITE = "#949494"
YELLOW = "#fcff76"
GREEN = "#00ff85"
CRIMSON = "#ff003d"
AMBER = "#ff8a00"

UPSTREAM = {
    "SpaceGrotesk[wght].ttf":
        "https://raw.githubusercontent.com/google/fonts/main/ofl/spacegrotesk/SpaceGrotesk%5Bwght%5D.ttf",
    "JetBrainsMono[wght].ttf":
        "https://raw.githubusercontent.com/google/fonts/main/ofl/jetbrainsmono/JetBrainsMono%5Bwght%5D.ttf",
}


def font(family: str, size: float, weight: int) -> ImageFont.FreeTypeFont:
    FONTS.mkdir(exist_ok=True)
    path = FONTS / family
    if not path.exists():
        path.write_bytes(urllib.request.urlopen(UPSTREAM[family]).read())
    f = ImageFont.truetype(str(path), int(round(size)))
    f.set_variation_by_axes([weight])
    return f


def sans(size: float, weight: int = 700) -> ImageFont.FreeTypeFont:
    return font("SpaceGrotesk[wght].ttf", size, weight)


def mono(size: float, weight: int = 400) -> ImageFont.FreeTypeFont:
    return font("JetBrainsMono[wght].ttf", size, weight)


def mono_fit(text: str, max_w: float, cap: float, track_ratio: float = 0.08
             ) -> tuple[ImageFont.FreeTypeFont, float]:
    """Largest mono size at which `text` still fits `max_w`, never above `cap`.

    Set by measurement rather than by a guessed point size: the same string has to sit inside a
    640px card and a 1280px banner, and a line that overflows is the one defect nobody forgives.
    """
    size = cap
    while size > 6:
        f = mono(size)
        if run_width([(text, f)], size * track_ratio) <= max_w:
            return f, size * track_ratio
        size -= cap / 40
    f = mono(6)
    return f, 6 * track_ratio


# ---------------------------------------------------------------- text with tracking
# Pillow has no letter-spacing, and the wordmark is 0.055em of it. Drawn per character on a shared
# baseline, so runs of different weight in one line still sit on the same line.

def run_width(runs: list[tuple[str, ImageFont.FreeTypeFont]], track: float) -> float:
    w = 0.0
    for text, f in runs:
        for ch in text:
            w += f.getlength(ch) + track
    return max(0.0, w - track)


def draw_runs(d: ImageDraw.ImageDraw, x: float, baseline: float,
              runs: list[tuple[str, ImageFont.FreeTypeFont]], fill: str, track: float) -> float:
    for text, f in runs:
        for ch in text:
            d.text((x, baseline), ch, font=f, fill=fill, anchor="ls")
            x += f.getlength(ch) + track
    return x


# ---------------------------------------------------------------- the mark
# site/src/components/Logo.astro, viewBox 0 0 48 48. Origin of the sweep is the bottom-left corner;
# the rings are arcs of r=14/26/38 struck from it, and the blip sits on the middle one.

def mark(d: ImageDraw.ImageDraw, x: float, y: float, size: float, solid: bool = False) -> None:
    u = size / 48.0                       # one viewBox unit in pixels
    stroke = max(1, round(1.0 * u))       # every stroke in the source is 1 unit
    dim = WHITE if solid else CARBON
    ring = WHITE if solid else YELLOW

    def px(vx: float, vy: float) -> tuple[float, float]:
        return x + vx * u, y + vy * u

    # instrument frame — inset half a unit so the stroke sits inside the box, as the SVG does
    x0, y0 = px(0.5, 0.5)
    x1, y1 = px(47.5, 47.5)
    d.rectangle([x0, y0, x1, y1], outline=WHITE, width=stroke)

    # three range rings, upper-right quadrant, centred on the bottom-left corner (4, 44)
    cx, cy = px(4, 44)
    for r_units, colour in ((38, dim), (26, ring), (14, dim)):
        r = r_units * u
        d.arc([cx - r, cy - r, cx + r, cy + r], 270, 360, fill=colour, width=stroke)

    # the sweep, ending exactly on the outer ring
    d.line([px(4, 44), px(33.1, 19.6)], fill=WHITE, width=stroke)

    # the blip: the only fill and the only unambiguous colour in the mark
    bx, by = px(23.9, 27.3)
    r = 3 * u
    d.ellipse([bx - r, by - r, bx + r, by + r], fill=GREEN)


def mark_round(d: ImageDraw.ImageDraw, side: float, solid: bool = False) -> None:
    """The same radar, recomposed for a circular crop.

    Fitting the square mark inside the inscribed circle works, but it costs 32% of the canvas and
    Telegram draws avatars at about 40px in a chat list — at that size the frame and three rings
    collapse into a smudge. So the crop itself becomes the instrument bezel: the sweep origin moves
    inside the circle, the rings grow to fill it, and everything survives at thumbnail size.

    The proportions are still Logo.astro's — rings at 38 / 26 / 14 and the sweep at 40 degrees,
    ending on the outer ring with the blip where it crosses the middle one.
    """
    c = side / 2
    # The web mark strikes its quiet rings in carbon, which is right for a 30px logo on a lit page
    # and wrong here: at 36px on a phone carbon is gone and the composition looks lopsided. Graphite
    # holds at that size and still sits below the white sweep, so the hierarchy is unchanged.
    dim = WHITE if solid else GRAPHITE
    ring = WHITE if solid else YELLOW
    stroke = max(1, round(side * 0.018))

    # bezel: inset far enough that the mask cannot bite into the stroke
    r_bezel = side * 0.465
    d.ellipse([c - r_bezel, c - r_bezel, c + r_bezel, c + r_bezel], outline=WHITE, width=stroke)

    # Origin down-left of centre and rings out to 0.44 — sized so the arc's two ends land at 0.31
    # of the side from the centre, just inside the bezel, and the quadrant fills the circle instead
    # of floating in it.
    off = side * 0.25
    ox, oy = c - off, c + off
    r_out = side * 0.44
    for ratio, colour in ((38, dim), (26, ring), (14, dim)):
        r = r_out * ratio / 38
        d.arc([ox - r, oy - r, ox + r, oy + r], 270, 360, fill=colour, width=stroke)

    ang = math.radians(40)                   # the sweep angle in the source
    d.line([(ox, oy), (ox + r_out * math.cos(ang), oy - r_out * math.sin(ang))],
           fill=WHITE, width=stroke)

    r_mid = r_out * 26 / 38
    bx, by = ox + r_mid * math.cos(ang), oy - r_mid * math.sin(ang)
    r_blip = side * 0.036
    d.ellipse([bx - r_blip, by - r_blip, bx + r_blip, by + r_blip], fill=GREEN)


# ---------------------------------------------------------------- pieces of the design system

def pill(d: ImageDraw.ImageDraw, x: float, baseline: float, text: str, colour: str,
         size: float) -> float:
    """`.badge` from tokens.css: 1px border in currentColor, radius 9999px, mono, tracked."""
    f = mono(size)
    track = size * 0.1
    pad_x, pad_y = size * 1.15, size * 0.42
    w = run_width([(text, f)], track)
    top, bottom = baseline - size * 0.82 - pad_y, baseline + size * 0.22 + pad_y
    h = bottom - top
    d.rounded_rectangle([x, top, x + w + pad_x * 2, bottom], radius=h / 2,
                        outline=colour, width=max(1, round(size / 9)))
    draw_runs(d, x + pad_x, baseline, [(text, f)], colour, track)
    return x + w + pad_x * 2


def meter(d: ImageDraw.ImageDraw, x: float, y: float, w: float, h: float,
          on: int, total: int) -> None:
    """`.meter`: a hairline box of segments, amber where lit. Conviction, drawn."""
    b = max(1, round(h / 8))
    d.rectangle([x, y, x + w, y + h], outline=WHITE, width=b)
    inner_x, inner_y = x + b * 2, y + b * 2
    inner_w, inner_h = w - b * 4, h - b * 4
    gap = b
    seg = (inner_w - gap * (total - 1)) / total
    for i in range(total):
        sx = inner_x + i * (seg + gap)
        d.rectangle([sx, inner_y, sx + seg, inner_y + inner_h],
                    fill=AMBER if i < on else CARBON)


def canvas(w: int, h: int) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    im = Image.new("RGB", (w * SS, h * SS), BLACK)
    return im, ImageDraw.Draw(im)


def save(im: Image.Image, name: str, w: int, h: int, where: pathlib.Path | None = None) -> None:
    out = im.resize((w, h), Image.LANCZOS)
    path = (where or HERE) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    out.save(path, "PNG", optimize=True)
    rel = path.relative_to(HERE.parent.parent)
    print(f"{str(rel):44} {w}x{h}  {path.stat().st_size // 1024} KB")


# ---------------------------------------------------------------- the assets

def avatar(name: str, side: int = 512, solid: bool = False) -> None:
    """Bot and channel profile photo.

    Telegram masks avatars to a circle, so the mark is drawn for one — see mark_round for why the
    square frame does not survive at chat-list size.
    """
    im, d = canvas(side, side)
    mark_round(d, side * SS, solid=solid)
    save(im, name, side, side)


def banner(name: str, w: int, h: int, note: str = "",
           where: pathlib.Path | None = None) -> None:
    """Wide artwork: description picture, /start photo, link preview.

    One left edge for everything below the lockup — a grid built from hairlines cannot afford two
    ragged margins — and the readout strip sits on the baseline the rule establishes.

    There used to be a footnote along the bottom edge. It went for two reasons: at preview size
    it read as a grey smudge, and it said "never trades", which stopped being true the day an
    execution engine went on the plan. With the bottom edge empty the whole block drops by `dy`
    so the content stays optically centred rather than clinging to the top of the card.
    """
    im, d = canvas(w, h)
    W, H = w * SS, h * SS
    pad = W * 0.055
    inner = W - pad * 2
    dy = H * 0.055

    m = H * 0.32
    mark(d, pad, H * 0.14 + dy, m)

    tx = pad + m + W * 0.032
    big = H * 0.125
    f7, f3 = sans(big, 700), sans(big, 300)
    track = big * 0.055
    base = H * 0.14 + dy + big * 0.92
    draw_runs(d, tx, base, [("FOMO ", f7), ("ROBINHOOD", f3)], WHITE, track)
    draw_runs(d, tx, base + big * 1.24, [("RADAR", f7)], WHITE, track)

    fm, tm = mono_fit("WHO THE GOOD TRADERS ARE BUYING, WHILE IT IS STILL EARLY",
                      inner, H * 0.042)
    draw_runs(d, pad, H * 0.60 + dy,
              [("WHO THE GOOD TRADERS ARE BUYING, WHILE IT IS STILL EARLY", fm)], WHITE, tm)

    # the hairline the whole system is built from
    d.line([pad, H * 0.685 + dy, W - pad, H * 0.685 + dy], fill=CARBON, width=max(1, round(H / 320)))

    # the readout strip: the verdict vocabulary in the three colours it is published in
    row = H * 0.795 + dy
    x = pad
    for text, colour in (("FOLLOW", GREEN), ("WATCH", YELLOW), ("DROP", CRIMSON)):
        x = pill(d, x, row, text, colour, H * 0.040) + W * 0.020
    meter_w = W * 0.095
    meter(d, x, row - H * 0.034, meter_w, H * 0.032, 7, 10)

    if note:
        # Right-aligned so the right margin matches the left one, but never wider than the room the
        # strip actually leaves — measured against the meter, not assumed.
        room = (W - pad) - (x + meter_w + W * 0.030)
        fn, tn = mono_fit(note, room, H * 0.040)
        draw_runs(d, W - pad - run_width([(note, fn)], tn), row, [(note, fn)], GRAPHITE, tn)

    save(im, name, w, h, where)


if __name__ == "__main__":
    avatar("tg-avatar-512.png")
    avatar("tg-avatar-solid-512.png", solid=True)
    banner("tg-description-640x360.png", 640, 360)
    banner("tg-start-1280x640.png", 1280, 640, note="/signals  /fresh  /token  /trader")
    # The link preview is written straight into the site so there is one file rather than two
    # copies that can drift; Base.astro imports it, and the build hashes the name so a new
    # rendering is a new URL — which is the only thing Telegram's preview cache respects. No
    # address on it: every place that shows a preview prints the domain underneath anyway.
    banner("og.png", 1200, 630, where=HERE.parent.parent / "site" / "src" / "assets")
