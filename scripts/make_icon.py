"""Draw the app icon from the same mark the sidebar uses.

Kept as a script rather than a checked-in binary nobody can edit: the icon is
derived from the logo, so when the logo changes this regenerates it instead of
someone hand-editing a .ico in a program they do not have.

The sidebar's clock keeps the real time. This one cannot, so it is frozen at
the pose the mark held before it could tell the time -- see ICON_HOUR below.

Drawn at 1024 and downsampled, because thin strokes alias badly if each size
is drawn at its own scale.
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

BG = (14, 16, 19, 255)        # near-black tile, legible on light and dark taskbars
FLAT = (90, 97, 104, 255)     # the market's line, and the dial around it
RISE = (46, 230, 160, 255)    # the edge
SIZES = (256, 128, 64, 48, 32, 24, 16)
MASTER = 1024

# Twenty-three minutes to four. The mark used to be a fixed shape -- a long
# stroke climbing from the market's line and a short one falling away from the
# peak -- and this is the nearest a real time gets to it: long hand down to
# the left, landing on the line, short hand out to the right. The sidebar's
# clock keeps the actual time; this one cannot, so it holds that pose.
ICON_HOUR = 3
ICON_MINUTE = 37


def _stroke(d: ImageDraw.ImageDraw, points, fill, width: float) -> None:
    """A polyline with round caps and joins.

    PIL's `joint="curve"` rounds the joins but leaves the ends square, and a
    square-ended diagonal at 16px looks like a broken shape rather than a line.
    Discs at every vertex are the cheap, exact fix.
    """
    d.line(points, fill=fill, width=int(round(width)), joint="curve")
    r = width / 2.0
    for x, y in points:
        d.ellipse([x - r, y - r, x + r, y + r], fill=fill)


def draw(size: int = MASTER) -> Image.Image:
    """The mark, in a dial, on the market's line.

    Laid out in fractions of the tile rather than on the SVG's 28-unit grid.
    The grid was fine when the mark was a free-standing polyline; once the
    strokes have to fit inside a dial they are much shorter, and a stroke
    width inherited from the old layout makes them read as blobs. Fractions
    keep the length-to-width ratio the original strokes had, which is about
    five to one and is most of why the mark looks like a mark.
    """
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=int(size * 0.23), fill=BG)

    centre = (0.5 * size, 0.43 * size)
    r = 0.375 * size
    # The dial. A ring, not a filled face: the tile is the face.
    d.ellipse([centre[0] - r, centre[1] - r, centre[0] + r, centre[1] + r],
              outline=FLAT, width=max(1, round(0.042 * size)))
    # The market's flat line, where it always was -- under everything else.
    _stroke(d, [(0.075 * size, 0.885 * size), (0.925 * size, 0.885 * size)],
            FLAT, 0.044 * size)

    def hand(degrees: float, length: float):
        a = math.radians(degrees)
        return (centre[0] + length * size * math.sin(a),
                centre[1] - length * size * math.cos(a))

    minute_angle = ICON_MINUTE * 6
    hour_angle = ((ICON_HOUR % 12) + ICON_MINUTE / 60) * 30
    _stroke(d, [centre, hand(hour_angle, 0.235)], RISE, 0.072 * size)
    _stroke(d, [centre, hand(minute_angle, 0.315)], RISE, 0.060 * size)

    # The dot that always joined the two strokes, ringed in tile colour so it
    # reads as a pivot rather than as a thickening where they meet.
    dot = 0.052 * size
    d.ellipse([centre[0] - dot, centre[1] - dot, centre[0] + dot, centre[1] + dot],
              fill=RISE, outline=BG, width=max(1, round(0.016 * size)))
    return img


# An .icns is a container of PNGs with a four-byte type per size. Written by
# hand because the alternative is `iconutil`, which exists only on macOS --
# and this has to run wherever the icon is regenerated, not only there.
ICNS_TYPES = (
    (b"icp4", 16), (b"icp5", 32), (b"icp6", 64),
    (b"ic07", 128), (b"ic08", 256), (b"ic09", 512), (b"ic10", 1024),
    (b"ic11", 32), (b"ic12", 64), (b"ic13", 256), (b"ic14", 512),
)


def write_icns(master: Image.Image, path: Path) -> None:
    import io
    import struct

    entries = []
    for ostype, size in ICNS_TYPES:
        buf = io.BytesIO()
        master.resize((size, size), Image.LANCZOS).save(buf, format="PNG")
        data = buf.getvalue()
        entries.append(ostype + struct.pack(">I", len(data) + 8) + data)
    body = b"".join(entries)
    path.write_bytes(b"icns" + struct.pack(">I", len(body) + 8) + body)


def main() -> int:
    master = draw()
    out = Path("assets")
    out.mkdir(exist_ok=True)
    master.resize((512, 512), Image.LANCZOS).save(out / "icon.png")
    master.save(out / "icon.ico", format="ICO", sizes=[(s, s) for s in SIZES])
    # macOS refuses a .ico outright: PyInstaller will only take an .icns there,
    # and its automatic conversion needs Pillow on the build machine, which a
    # CI runner does not have. Shipping both formats is the fix that does not
    # depend on what happens to be installed where.
    write_icns(master, out / "icon.icns")
    print(f"wrote icon.ico, icon.icns and icon.png in {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
