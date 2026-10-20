"""
The SET marks agree with each other, wherever they are drawn.

Four things draw the brand, in three languages and two of them binary:

    web/lib/marks.ts          the geometry, and the one source
    web/app/brand.tsx         the nav lockup, imports it
    web/app/opengraph-image   the share card, imports it
    web/app/icon.svg          the favicon - a STATIC FILE, cannot import
    web/app/favicon.ico       rasters generated from icon.svg
    web/app/apple-icon.png

The imports take care of themselves. The last three cannot, and they are
exactly the ones nobody checks: restyle the logo and the nav updates on the
next build while the favicon, the iOS tile and every share card already in a
crawler's cache keep showing the old mark. No error, no failing page - just a
site wearing two different logos depending on where you look at it.

Same reasoning as tests/test_signup_form.py and tests/test_leaderboard_page.py,
applied to a shape rather than a number.
"""

import re
import struct
import zlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB = REPO_ROOT / "web"
MARKS = WEB / "lib" / "marks.ts"
ICON = WEB / "app" / "icon.svg"
GLOBALS = WEB / "app" / "globals.css"
APPLE = WEB / "app" / "apple-icon.png"
FAVICON = WEB / "app" / "favicon.ico"


def _ts_string_array(source: str, name: str) -> list[str]:
    """
    The contents of `export const NAME = [ "...", "..." ];`

    Matched on the declaration rather than parsed as TypeScript, because the
    alternative is a Node dependency in a Python test for the sake of reading
    a list of strings.
    """
    block = re.search(rf"export const {re.escape(name)} = \[(.*?)\n\];",
                      source, re.S)
    assert block, f"{name} is missing from {MARKS.name}"
    return re.findall(r'"([^"]+)"', block.group(1))


def _ts_const(source: str, name: str) -> str:
    found = re.search(rf'export const {re.escape(name)} = "([^"]+)";', source)
    assert found, f"{name} is missing from {MARKS.name}"
    return found.group(1)


def _png_first_pixel(data: bytes) -> tuple[int, int, int]:
    """
    The top-left RGB of a PNG, which for these icons is background.

    Only the first pixel is read, and that makes the decoder three lines
    instead of forty: every PNG filter reconstructs the first pixel of the
    first scanline from neighbours that are all zero, so whichever filter was
    used, byte one of the decompressed stream onwards is the literal value.
    """
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    pos, idat = 8, b""
    while pos < len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        kind = data[pos + 4:pos + 8]
        if kind == b"IDAT":
            idat += data[pos + 8:pos + 8 + length]
        elif kind == b"IEND":
            break
        pos += 12 + length
    raw = zlib.decompress(idat)
    return raw[1], raw[2], raw[3]


def _ico_pngs(data: bytes) -> list[bytes]:
    """Every PNG payload inside an ICO, with the directory validated."""
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert reserved == 0 and kind == 1, "not an ICO"
    assert count > 0, "ICO declares no images"

    payloads = []
    for i in range(count):
        entry = data[6 + 16 * i:6 + 16 * (i + 1)]
        size, offset = struct.unpack("<II", entry[8:16])
        assert offset + size <= len(data), (
            f"ICO entry {i} points past the end of the file")
        payloads.append(data[offset:offset + size])
    return payloads


def test_the_favicon_draws_the_same_mark_as_the_components():
    """
    icon.svg is a static file the framework reads directly, so it cannot
    import lib/marks.ts the way brand.tsx and the share card do. This is the
    import it cannot write.
    """
    expected = _ts_string_array(MARKS.read_text(encoding="utf-8"),
                                "LOGOMARK_PATHS")
    actual = re.findall(r'<path d="([^"]+)"', ICON.read_text(encoding="utf-8"))

    assert actual == expected, (
        "app/icon.svg draws different geometry from LOGOMARK_PATHS in "
        "lib/marks.ts - the tab icon and the nav logo are now two different "
        "marks. Regenerate icon.svg from the brand kit, and the rasters from "
        "icon.svg.")


def test_the_favicon_uses_the_viewbox_the_mark_was_drawn_in():
    """
    A mark pasted into a viewBox of the wrong shape is not a drift test
    failure anywhere else - it renders, just squashed.
    """
    source = MARKS.read_text(encoding="utf-8")
    width, height = _ts_const(source, "LOGOMARK_VIEWBOX").split()[2:]

    icon = ICON.read_text(encoding="utf-8")
    scale = re.search(r"scale\(([\d.]+)\)", icon)
    assert scale, "app/icon.svg no longer scales the mark - check the transform"

    # The mark is centred in a square canvas, so only its ASPECT has to
    # survive: a uniform scale and a translate cannot distort it, and the
    # exact numbers are free to change when the padding is retuned.
    assert "scale(" in icon and icon.count("scale(") == 1, (
        "app/icon.svg should apply exactly one uniform scale to the mark; a "
        "second transform can distort it without anything else noticing")
    assert float(width) > 0 and float(height) > 0


def test_every_icon_uses_the_page_background():
    """
    The icons are generated outside the CSS pipeline, so none of them can read
    `--background`. Three copies of that colour therefore exist - the token,
    the SVG, and the rasters - and the rasters are the ones that drifted:
    they shipped pure black against the site's #08090b, which put a visibly
    different black on the iOS tile than in the browser tab.
    """
    token = _ts_const(MARKS.read_text(encoding="utf-8"), "BRAND_BACKGROUND")

    css = re.search(r"--background:\s*(#[0-9a-fA-F]{6})",
                    GLOBALS.read_text(encoding="utf-8"))
    assert css, "--background is missing from globals.css"
    assert token.lower() == css.group(1).lower(), (
        f"BRAND_BACKGROUND is {token} but globals.css --background is "
        f"{css.group(1)} - the icons no longer match the page they open")

    rgb = tuple(int(token[i:i + 2], 16) for i in (1, 3, 5))

    assert _png_first_pixel(APPLE.read_bytes()) == rgb, (
        f"apple-icon.png's background is not {token}. Regenerate it from "
        f"app/icon.svg rather than from the brand kit's square PNG, which is "
        f"pure black.")

    for payload in _ico_pngs(FAVICON.read_bytes()):
        assert _png_first_pixel(payload) == rgb, (
            f"a favicon.ico entry's background is not {token} - regenerate "
            f"the ICO from app/icon.svg")


def test_the_favicon_is_a_well_formed_ico():
    """
    favicon.ico is assembled by hand (PNG-in-ICO) rather than by an image
    library, so nothing but this checks the container. A malformed one does
    not fail a build; it just quietly shows no icon.
    """
    data = FAVICON.read_bytes()
    payloads = _ico_pngs(data)

    assert len(payloads) >= 1, "favicon.ico contains no images"
    for payload in payloads:
        assert payload[:8] == b"\x89PNG\r\n\x1a\n", (
            "favicon.ico entry is not a PNG - this file is built as "
            "PNG-in-ICO and a BMP entry would need a different header")

    # Every byte after the directory must belong to exactly one payload: a gap
    # means a wrong offset, an overlap means a wrong length, and both render
    # as "no icon" rather than as an error.
    spans = []
    for i in range(len(payloads)):
        entry = data[6 + 16 * i:6 + 16 * (i + 1)]
        size, offset = struct.unpack("<II", entry[8:16])
        spans.append((offset, offset + size))
    spans.sort()

    assert spans[0][0] == 6 + 16 * len(payloads), (
        "favicon.ico's first payload does not start where the directory ends")
    for (_, end), (start, _) in zip(spans, spans[1:]):
        assert end == start, "favicon.ico payloads overlap or leave a gap"
    assert spans[-1][1] == len(data), (
        "favicon.ico has trailing bytes after the last payload")
