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
GENERATOR = WEB / "scripts" / "build-icons.mjs"


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


def _png_rgb(data: bytes) -> tuple[int, int, bytes]:
    """
    A PNG's pixels, as 8-bit RGB with the row filtering undone.

    Written out rather than imported: these icons are the only images in the
    repository, and Pillow is a megabyte of C to measure six numbers. Only
    the shapes scripts/build-icons.mjs emits are handled - 8-bit RGB and
    RGBA - and anything else fails loudly instead of being misread as pixels,
    which is the failure mode that matters here. An indexed PNG read as
    truecolour returns palette INDICES that look like plausible dark
    colours, so a background assertion against one passes or fails for
    reasons unconnected to what the icon looks like.
    """
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"

    width = height = depth = colour = None
    idat, pos = b"", 8
    while pos < len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            (width, height, depth, colour,
             packing, filtering, interlace) = struct.unpack(">IIBBBBB", body[:13])
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
        pos += 12 + length

    assert depth == 8, f"PNG bit depth {depth} is not handled"
    assert colour in (2, 6), f"PNG colour type {colour} is not handled"
    assert packing == 0 and filtering == 0, "unknown PNG compression scheme"
    # Adam7 stores seven subimages, so the scanline walk below reads pixel
    # bytes as row filters and returns noise. It happens to trip the filter
    # assertion today, which is luck rather than a check: adding
    # `progressive: true` to scripts/build-icons.mjs is the one-word change
    # that would reach it.
    assert interlace == 0, "interlaced PNGs are not handled"
    step = 3 if colour == 2 else 4
    stride = width * step

    raw = zlib.decompress(idat)
    out, prior = bytearray(), bytearray(stride)
    for y in range(height):
        start = y * (stride + 1)
        kind, line = raw[start], bytearray(raw[start + 1:start + 1 + stride])
        assert 0 <= kind <= 4, f"unknown PNG row filter {kind}"
        for i in range(stride if kind else 0):   # filter 0 is already pixels
            left = line[i - step] if i >= step else 0
            up = prior[i]
            if kind == 1:
                line[i] = (line[i] + left) & 0xFF
            elif kind == 2:
                line[i] = (line[i] + up) & 0xFF
            elif kind == 3:
                line[i] = (line[i] + (left + up) // 2) & 0xFF
            elif kind == 4:
                corner = prior[i - step] if i >= step else 0
                guess = left + up - corner
                near = min((abs(guess - v), n)
                           for n, v in enumerate((left, up, corner)))[1]
                line[i] = (line[i] + (left, up, corner)[near]) & 0xFF
        prior = line
        for x in range(width):
            out += line[x * step:x * step + 3]
    return width, height, bytes(out)


def _mark_bounds(width: int, height: int, rgb: bytes) -> tuple[int, int, int, int]:
    """
    The box the white mark occupies, in pixels: (left, top, width, height).

    Thresholded on the red channel at the midpoint, which splits cleanly
    because the only two colours in these icons are the near-black
    background and a white mark. Anti-aliased edges move each bound by less
    than a pixel, hence the tolerance at the call site rather than here.
    """
    xs, ys = [], []
    for y in range(height):
        row = y * width
        for x in range(width):
            if rgb[(row + x) * 3] > 128:
                xs.append(x)
                ys.append(y)
    assert xs, "found no mark in this icon - it is a blank rectangle"
    return min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1


def _js_number_array(source: str, name: str) -> list[int]:
    """The contents of `const NAME = [1, 2];` in the generator script."""
    block = re.search(rf"const {re.escape(name)} = \[([^\]]*)\]", source)
    assert block, f"{name} is missing from {GENERATOR.name}"
    return [int(n) for n in re.findall(r"\d+", block.group(1))]


def _corner(data: bytes) -> tuple[int, int, int]:
    """The top-left pixel, which for these icons is background."""
    _, _, rgb = _png_rgb(data)
    return rgb[0], rgb[1], rgb[2]


def _icon_transform() -> tuple[float, float, float, float]:
    """
    icon.svg's canvas size and the transform it places the mark with:
    (canvas, translate x, translate y, scale).
    """
    icon = ICON.read_text(encoding="utf-8")

    box = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', icon)
    assert box, "app/icon.svg has no viewBox"
    assert box.group(1) == box.group(2), (
        "app/icon.svg is no longer square - the icon formats it is rasterised "
        "into all are, so a non-square canvas would be letterboxed")

    move = re.search(r"translate\(([\d.]+)[ ,]+([\d.]+)\)", icon)
    scale = re.search(r"scale\(([\d.]+)\)", icon)
    assert move and scale, "app/icon.svg no longer translates and scales the mark"
    return (float(box.group(1)), float(move.group(1)),
            float(move.group(2)), float(scale.group(1)))


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

    assert _corner(APPLE.read_bytes()) == rgb, (
        f"apple-icon.png's background is not {token}. Regenerate it from "
        f"app/icon.svg rather than from the brand kit's square PNG, which is "
        f"pure black.")

    for payload in _ico_pngs(FAVICON.read_bytes()):
        assert _corner(payload) == rgb, (
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

    # The directory says how big each image is, and nothing makes that true.
    # A browser picks an entry by the DECLARED size and then decodes whatever
    # is actually there: an entry advertising 32x32 over a 16px PNG is chosen
    # for a 32px slot and drawn blurry. The geometry test cannot see this
    # either - it scales its prediction by the PNG's own width, so a
    # half-sized image passes as a correctly drawn small one.
    declared = []
    for i, payload in enumerate(payloads):
        entry = data[6 + 16 * i:6 + 16 * (i + 1)]
        width, height, _ = _png_rgb(payload)
        assert (entry[0] or 256, entry[1] or 256) == (width, height), (
            f"favicon.ico entry {i} is declared "
            f"{entry[0] or 256}x{entry[1] or 256} but holds a "
            f"{width}x{height} PNG")
        declared.append(width)

    wanted = _js_number_array(GENERATOR.read_text(encoding="utf-8"), "ICO_SIZES")
    assert sorted(declared) == sorted(wanted), (
        f"favicon.ico holds {sorted(declared)} but scripts/build-icons.mjs "
        f"generates {sorted(wanted)} - the committed file is not this "
        f"script's output. Run `npm run icons` in web/.")

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


def test_the_rasters_are_the_same_drawing_as_the_vector():
    """
    apple-icon.png and favicon.ico draw the mark at the size and position
    icon.svg puts it at.

    This is the check that was missing, and it was missing for a reason worth
    writing down: the test above pins the rasters' background COLOUR, which
    reads like a check that they were made from icon.svg, and is not one. Both
    were in fact made by shrinking the brand kit's 1000x1000 square PNG, whose
    padding is tighter - so the mark shipped ~5% larger on the iOS home screen
    and ~12% wider at 32px than in the browser tab, while every existing
    assertion passed. Two sizes of one logo, depending where you looked.

    Predicted from LOGOMARK_VIEWBOX through icon.svg's own transform rather
    than from hardcoded pixel counts, so retuning the padding moves the
    expectation with it instead of failing this test.

    The 16px ICO entry is along for the ride. A mark 12% too wide at that
    size lands on the same integer pixels as a correct one - the bug this
    test exists for is literally invisible there - so the 32px entry and
    apple-icon.png are what actually carry it. Checked anyway, because a
    gross error would still show.
    """
    canvas, tx, ty, scale = _icon_transform()
    source = MARKS.read_text(encoding="utf-8")
    vb_w, vb_h = (float(n)
                  for n in _ts_const(source, "LOGOMARK_VIEWBOX").split()[2:])

    # One pixel, flat, NOT scaled to the icon. Each measured bound is an
    # integer taken from a thresholded anti-aliased edge, so it carries up to
    # half a pixel; width and height combine two bounds, so a pixel is the
    # quantisation bound for all four numbers at every size. Measured worst
    # case on correct rasters is 0.632.
    #
    # It was 2px, which is where the first version of this test went wrong:
    # at 180px that caught the apple-icon error (4.4px) and read like a
    # working test, while the SAME bug in favicon.ico measures 1.368px at
    # 32px and sailed through. The suite passed on a broken ICO. A tolerance
    # in pixels must therefore be a statement about measurement noise, which
    # does not grow with the icon, rather than a fraction of the artwork.
    TOLERANCE = 1

    rasters = [("apple-icon.png", APPLE.read_bytes())]
    rasters += [(f"favicon.ico entry {i}", payload)
                for i, payload in enumerate(_ico_pngs(FAVICON.read_bytes()))]

    for name, data in rasters:
        width, height, rgb = _png_rgb(data)
        assert width == height, f"{name} is not square ({width}x{height})"

        px = width / canvas       # canvas units to pixels at this size
        expected = (tx * px, ty * px, vb_w * scale * px, vb_h * scale * px)
        actual = _mark_bounds(width, height, rgb)

        for label, want, got in zip(("left", "top", "width", "height"),
                                    expected, actual):
            assert abs(want - got) <= TOLERANCE, (
                f"{name} ({width}px) draws the mark's {label} at {got}px, but "
                f"icon.svg puts it at {want:.1f}px. The rasters are not this "
                f"icon.svg. Regenerate them:\n"
                f"    cd web && node scripts/build-icons.mjs")
