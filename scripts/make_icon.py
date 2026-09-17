"""Draw the app icon from the same mark the sidebar uses.

Kept as a script rather than a checked-in binary nobody can edit: the icon is
derived from the logo, so when the logo changes this regenerates it instead of
someone hand-editing a .ico in a program they do not have.

The sidebar's clock keeps the real time. This one cannot, so it is frozen at
ten to two -- see ICON_HOUR below.

Drawn at 1024 and downsampled, because thin strokes alias badly if each size
is drawn at its own scale.
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

BG = (14, 16, 19, 255)        # near-black tile, legible on light and dark taskbars
RIM = (90, 97, 104, 255)      # the dial: the market, the part that never moves
HOUR = (214, 221, 228, 255)   # the short hand
MINUTE = (46, 230, 160, 255)  # the long hand: the edge
SIZES = (256, 128, 64, 48, 32, 24, 16)
MASTER = 1024

# Ten to two. The pose is the one every watch is photographed in, for the
# reason it is worth copying: both hands above the centre, neither hiding the
# other, and the gap between them wide enough to read at 16 pixels. It is also
# the arrangement the app asks for -- long hand left, short hand right.
ICON_HOUR = 1
ICON_MINUTE = 50


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
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    u = size / 28.0                                  # the SVG's 28-unit grid

    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=int(size * 0.23), fill=BG)

    # Inset from the tile edge. A mark that runs to the corners reads as
    # cropped once Windows rounds the thumbnail.
    def pt(x, y):
        return (size * 0.18 + x * u * 0.64, size * 0.20 + y * u * 0.64)

    def hand(degrees: float, length: float):
        """The far end of a hand, `length` grid units from the centre at
        `degrees` clockwise from twelve."""
        a = math.radians(degrees)
        return pt(14 + length * math.sin(a), 14 - length * math.cos(a))

    centre = pt(14, 14)
    r = 11.6 * u * 0.64
    # The dial. Drawn as a ring rather than a filled disc so the tile shows
    # through: a filled face would need a second colour and buys nothing.
    d.ellipse([centre[0] - r, centre[1] - r, centre[0] + r, centre[1] + r],
              outline=RIM, width=int(round(1.8 * u)))

    minute_angle = ICON_MINUTE * 6
    hour_angle = ((ICON_HOUR % 12) + ICON_MINUTE / 60) * 30
    # Short and heavy, long and light -- the convention that tells the two
    # hands apart without a number anywhere on the dial.
    _stroke(d, [centre, hand(hour_angle, 6.2)], HOUR, 3.0 * u)
    _stroke(d, [centre, hand(minute_angle, 9.1)], MINUTE, 2.6 * u)

    # Just big enough to look like a pinned centre rather than a join. Any
    # larger and it merges with the hand it shares a colour with, which at 16px
    # turns the middle of the dial into one green blob.
    pip = 1.7 * u * 0.64 + 0.35 * u
    d.ellipse([centre[0] - pip, centre[1] - pip, centre[0] + pip, centre[1] + pip],
              fill=MINUTE)
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
