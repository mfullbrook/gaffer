"""Build the Gaffer logo SVGs (and PNG previews) from hand-drawn geometry.

The icon is drawn once on a 512x512 grid. Everything that is symmetrical is
defined for the left half only and mirrored, so the two sides always match.
The wordmark is the word "gaffer" set in Jost ExtraBold and converted to outlines,
so the finished SVGs need no fonts.

Usage:
    uv run --with fonttools --with resvg-py python logos/build_logo.py PATH/TO/Jost[wght].ttf

Jost is OFL-licensed: https://github.com/google/fonts/tree/main/ofl/jost
"""

import sys
from pathlib import Path

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

YELLOW = "#FFC400"
INK = "#111111"
WHITE = "#FFFFFF"
TILE = "#111111"

STROKE = 14
# Width of the light border drawn around the head on dark backgrounds.
HALO = 12

FACE = '<circle cx="256" cy="300" r="158"/>'
DOME = '<path d="M94,200 C94,112 168,54 256,54 C344,54 418,112 418,200 Z"/>'
RIDGE = '<rect x="210" y="40" width="92" height="160" rx="18"/>'
BRIM = '<path d="M86,182 Q256,158 426,182 C454,186 456,236 426,240 Q256,218 86,240 C56,236 58,186 86,182 Z"/>'
# Left half only; the right half is its mirror image.
MOUSTACHE = """<path d="M258,344
           C240,326 212,320 192,328
           C174,335 160,348 150,354
           C142,359 130,360 124,352
           C119,345 121,336 130,332
           C120,326 104,330 96,344
           C86,362 92,390 112,402
           C138,416 178,410 206,398
           C226,390 244,386 258,386 Z"/>"""

MIRROR = 'transform="matrix(-1 0 0 1 512 0)"'


def both_sides(left: str) -> str:
    return f"<g>{left}</g><g {MIRROR}>{left}</g>"


def icon_group(halo: str | None = None) -> str:
    """The foreman's head, on the 512 grid.

    With `halo`, the whole silhouette gets a border in that colour so the black
    outlines and moustache stay visible on a dark background.
    """
    parts = []
    if halo:
        parts.append(
            f'<g fill="{halo}" stroke="{halo}" stroke-width="{STROKE + 2 * HALO}">'
            f"{FACE}{DOME}{RIDGE}{BRIM}{both_sides(MOUSTACHE)}</g>"
        )
    parts.append(f"""<g stroke="{INK}" stroke-width="{STROKE}">
  <g fill="{WHITE}">{FACE}</g>
  <g fill="{YELLOW}">{DOME}{RIDGE}</g>
  {both_sides('<path d="M158,104 C166,130 170,156 170,184" fill="none" stroke-width="11"/>')}
  <g fill="{YELLOW}">{BRIM}</g>
  <path d="M150,202 Q256,190 362,202" fill="none" stroke-width="10"/>
  <path d="M232,94 L254,114 L232,134" fill="none" stroke-width="11"/>
  <path d="M262,136 H284" fill="none" stroke-width="11"/>
  {both_sides('<path d="M146,252 Q148,263 160,264 L222,278" fill="none" stroke-width="17"/>')}
  <g fill="{INK}" stroke="none">{both_sides('<circle cx="194" cy="302" r="15"/>')}{both_sides(MOUSTACHE)}</g>
</g>""")
    return f'<g stroke-linejoin="round" stroke-linecap="round">{"".join(parts)}</g>'


# Tight bounds of the icon artwork on the 512 grid (x, y, w, h), strokes included.
ICON_BOX = (54, 32, 404, 434)
# The same, with the halo border.
HALO_BOX = (ICON_BOX[0] - HALO, ICON_BOX[1] - HALO, ICON_BOX[2] + 2 * HALO, ICON_BOX[3] + 2 * HALO)


def wordmark(font_path: Path, x_height: float, tracking: float = 0.012):
    """Return (svg path data, width, ascent, descent) for "gaffer" at the given x-height.

    Coordinates have the baseline at y=0 and grow downwards (SVG convention).
    """
    font = TTFont(font_path)
    glyphs = font.getGlyphSet(location={"wght": 800})
    cmap = font.getBestCmap()
    upm = font["head"].unitsPerEm
    scale = x_height / font["OS/2"].sxHeight

    pen = SVGPathPen(glyphs)
    cursor = 0.0
    for ch in "gaffer":
        glyph = glyphs[cmap[ord(ch)]]
        glyph.draw(TransformPen(pen, (scale, 0, 0, -scale, cursor, 0)))
        cursor += (glyph.width + tracking * upm) * scale
    width = cursor - tracking * upm * scale
    ascent = font["OS/2"].sTypoAscender * scale
    descent = -font["OS/2"].sTypoDescender * scale
    return pen.getCommands(), width, ascent, descent


def svg(view_w: float, view_h: float, body: str, title: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {view_w:g} {view_h:g}" '
        f'width="{view_w:g}" height="{view_h:g}" role="img" aria-label="{title}">\n'
        f"<title>{title}</title>\n{body}\n</svg>\n"
    )


def build_icon() -> str:
    x, y, w, h = ICON_BOX
    side = max(w, h) + 40
    dx = (side - w) / 2 - x
    dy = (side - h) / 2 - y
    return svg(side, side, f'<g transform="translate({dx:g} {dy:g})">{icon_group()}</g>', "Gaffer")


def build_app_icon() -> str:
    size = 1024
    x, y, w, h = HALO_BOX
    s = (size * 0.80) / max(w, h)
    dx = (size - w * s) / 2 - x * s
    dy = (size - h * s) / 2 - y * s
    body = (
        f'<rect width="{size}" height="{size}" rx="224" fill="{TILE}"/>\n'
        f'<g transform="translate({dx:g} {dy:g}) scale({s:g})">{icon_group(halo=WHITE)}</g>'
    )
    return svg(size, size, body, "Gaffer")


def build_logo(font_path: Path, text_fill: str, background: str | None) -> str:
    x, y, w, h = ICON_BOX
    pad = 40 + (HALO if background else 0)
    gap = 44
    x_height = h * 0.31
    path_d, text_w, _, descent = wordmark(font_path, x_height)
    # Sit the x-height band level with the eyes and moustache.
    baseline = pad + h * 0.80
    view_w = pad + w + gap + text_w + pad
    view_h = max(pad + h + pad, baseline + descent + pad)
    parts = []
    if background:
        parts.append(f'<rect width="{view_w:g}" height="{view_h:g}" rx="48" fill="{background}"/>')
    halo = WHITE if background else None
    parts.append(f'<g transform="translate({pad - x:g} {pad - y:g})">{icon_group(halo)}</g>')
    parts.append(
        f'<path transform="translate({pad + w + gap:g} {baseline:g})" fill="{text_fill}" d="{path_d}"/>'
    )
    return svg(round(view_w), round(view_h), "\n".join(parts), "Gaffer")


def render_png(svg_text: str, out: Path, width: int) -> None:
    import resvg_py

    png = resvg_py.svg_to_bytes(svg_string=svg_text, width=width)
    out.write_bytes(bytes(png))


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    font_path = Path(sys.argv[1])
    here = Path(__file__).parent
    svg_dir = here / "svg"
    png_dir = here / "png"
    svg_dir.mkdir(exist_ok=True)
    png_dir.mkdir(exist_ok=True)

    outputs = {
        "gaffer-icon": (build_icon(), [1024, 256, 64, 32]),
        "gaffer-app-icon": (build_app_icon(), [1024, 256, 64, 32]),
        "gaffer-logo": (build_logo(font_path, INK, None), [2048]),
        "gaffer-logo-dark": (build_logo(font_path, WHITE, TILE), [2048]),
    }
    for name, (text, widths) in outputs.items():
        (svg_dir / f"{name}.svg").write_text(text)
        for width in widths:
            suffix = "" if width == widths[0] else f"-{width}"
            render_png(text, png_dir / f"{name}{suffix}.png", width)
        print(f"wrote {name}")


if __name__ == "__main__":
    main()
