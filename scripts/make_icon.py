"""Draw the app icon from the same mark the sidebar uses.

Kept as a script rather than a checked-in binary nobody can edit: the icon is
derived from the logo, so when the logo changes this regenerates it instead of
someone hand-editing a .ico in a program they do not have.

Drawn at 1024 and downsampled, because the thin rising line aliases badly if
each size is drawn at its own scale.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

BG = (14, 16, 19, 255)        # near-black tile, legible on light and dark taskbars
FLAT = (90, 97, 104, 255)     # the market's line
RISE = (46, 230, 160, 255)    # the edge
SIZES = (256, 128, 64, 48, 32, 24, 16)
MASTER = 1024


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

    # The market's flat line, then the edge climbing away from it. The flat one
    # is thinner: it is the reference, not the subject.
    _stroke(d, [pt(2, 20), pt(26, 20)], FLAT, 1.5 * u)
    _stroke(d, [pt(2, 20), pt(13, 6), pt(26, 12.5)], RISE, 2.6 * u)

    r = 2.9 * u
    cx, cy = pt(13, 6)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=RISE)
    # A ring of tile colour separates the dot from the line meeting under it.
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=BG, width=int(0.75 * u))
    return img


def main() -> int:
    master = draw()
    out = Path("assets")
    out.mkdir(exist_ok=True)
    master.resize((512, 512), Image.LANCZOS).save(out / "icon.png")
    master.save(out / "icon.ico", format="ICO",
                sizes=[(s, s) for s in SIZES])
    # macOS wants its own container; PyInstaller accepts a .icns or a .png.
    print(f"wrote {out / 'icon.ico'} and {out / 'icon.png'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
