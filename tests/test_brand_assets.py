"""
The SET marks agree with each other, wherever they are drawn.

Everything that draws the brand comes from one place:

    web/lib/marks.ts              the geometry and the colours, the source
    web/app/brand.tsx             the nav lockup - imports it
    web/app/opengraph-image.tsx   the share card - imports it
    web/scripts/build-icons.mjs   generates the three below from it
    web/app/icon.svg              the browser tab
    web/app/favicon.ico           browsers that will not take an SVG icon
    web/app/apple-icon.png        the iOS home screen

The two imports take care of themselves. The last three are committed files
that nothing re-checks at build time, and they are exactly the ones nobody
looks at: restyle the logo and the nav updates on the next deploy while the
favicon, the iOS tile and every share card in a crawler's cache keep showing
the old mark. No error, no failing page - just a site wearing two different
logos depending on where you look at it.

That is not hypothetical. It shipped: the rasters were once made by shrinking
the brand kit's square PNG rather than from icon.svg, and the mark was ~5%
larger on the iOS tile and 12% wider at 32px than in the browser tab for as
long as nobody measured it.

Same reasoning as tests/test_signup_form.py and tests/test_leaderboard_page.py,
applied to a shape and a colour rather than a number.
"""

import re
import struct
import zlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB = REPO_ROOT / "web"
MARKS = WEB / "lib" / "marks.ts"
GENERATOR = WEB / "scripts" / "build-icons.mjs"
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
    # `=\s*"` rather than `= "`, matching how build-icons.mjs reads the same
    # file: a formatter wrapping the line would otherwise leave the script
    # parsing it happily and this test reporting the constant as missing.
    found = re.search(rf'export const {re.escape(name)} =\s*"([^"]+)";', source)
    assert found, f"{name} is missing from {MARKS.name}"
    return found.group(1)


def _js_number(name: str) -> float:
    """A `const NAME = <number>;` from the generator script."""
    found = re.search(rf"const {re.escape(name)} = ([\d.]+);",
                      GENERATOR.read_text(encoding="utf-8"))
    assert found, f"{name} is missing from {GENERATOR.name}"
    return float(found.group(1))


def _js_number_array(name: str) -> list[int]:
    """The contents of `const NAME = [1, 2];` in the generator script."""
    block = re.search(rf"const {re.escape(name)} = \[([^\]]*)\]",
                      GENERATOR.read_text(encoding="utf-8"))
    assert block, f"{name} is missing from {GENERATOR.name}"
    return [int(n) for n in re.findall(r"\d+", block.group(1))]


def _logomark_viewbox() -> tuple[float, float]:
    """The mark's own width and height, from LOGOMARK_VIEWBOX."""
    box = _ts_const(MARKS.read_text(encoding="utf-8"), "LOGOMARK_VIEWBOX")
    width, height = (float(n) for n in box.split()[2:])
    assert width > 0 and height > 0, f"LOGOMARK_VIEWBOX is degenerate: {box!r}"
    return width, height


def _layout(margin: float) -> tuple[float, float, float]:
    """
    Where the mark sits on the canvas: (scale, x, y).

    Re-derived here rather than read back out of the generated SVG, which
    would only prove the file agrees with itself. If build-icons.mjs changes
    how it places the mark, this disagrees with what it wrote and the suite
    says so - which is the point, since that is a deliberate change someone
    should have to make in both places.
    """
    canvas = _js_number("CANVAS")
    width, height = _logomark_viewbox()
    scale = (canvas - 2 * margin) / height
    return scale, (canvas - width * scale) / 2, margin


def _png_rgba(data: bytes) -> tuple[int, int, bytes]:
    """
    A PNG's pixels, as 8-bit RGBA with the row filtering undone.

    Written out rather than imported: these icons are the only images in the
    repository, and Pillow is a megabyte of C to measure six numbers. Only
    the shapes build-icons.mjs emits are handled - 8-bit RGB and RGBA - and
    anything else fails loudly instead of being misread as pixels, which is
    the failure mode that matters here. An indexed PNG read as truecolour
    returns palette INDICES that look like plausible dark colours, so an
    assertion against one passes or fails for reasons unconnected to what
    the icon looks like.
    """
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"

    width = height = depth = colour = None
    packing = filtering = interlace = None
    idat, pos = b"", 8
    while pos < len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            (width, height, depth, colour,
             packing, filtering, interlace) = struct.unpack(">IIBBBBB",
                                                            body[:13])
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
    # `progressive: true` to build-icons.mjs is the one-word change that
    # would reach it.
    assert interlace == 0, "interlaced PNGs are not handled"

    step = 3 if colour == 2 else 4
    stride = width * step

    raw = zlib.decompress(idat)
    assert len(raw) == height * (stride + 1), (
        "PNG pixel data is the wrong length for its IHDR - truncated file?")

    out, prior = bytearray(), bytearray(stride)
    for y in range(height):
        start = y * (stride + 1)
        kind, line = raw[start], bytearray(raw[start + 1:start + 1 + stride])
        assert kind <= 4, f"unknown PNG row filter {kind}"
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
            pixel = line[x * step:x * step + step]
            out += pixel if step == 4 else pixel + b"\xff"
    return width, height, bytes(out)


def _mark_bounds(width: int, height: int, rgba: bytes) -> tuple[int, ...]:
    """
    The box the mark occupies, in pixels: (left, top, width, height).

    "Mark" means anything far enough from the top-left pixel, which on every
    icon here is ground - transparent on the favicon, the brand near-black on
    the Apple tile. Measuring against the corner rather than against a fixed
    colour is what lets one function read both: the favicon's mark differs
    from its ground almost entirely in ALPHA, and a luminance test sees a
    transparent black canvas and a near-black mark as the same thing.

    Both are PREMULTIPLIED first, which is not a detail. Compare the stored
    channels and the answer depends on the mark's colour: a 10%-covered pixel
    of a WHITE mark is stored (255, 255, 255, 26), whose raw distance from a
    transparent ground is 255, so every faint edge pixel counts as mark and
    each bound slides out to the far side of the anti-aliasing. The icons are
    near-black today and it happens not to bite - but web/README.md names
    shipping the light mark in favicon.ico as the way to close its one gap,
    and that change alone would fail this test against a file the generator
    had just correctly produced, advising the reader to regenerate it.
    Premultiplied, that same pixel is (26, 26, 26, 26) and reads as the 10%
    coverage it is, whatever colour the mark happens to be.

    The halfway threshold then puts the bound on the geometric edge; a pixel
    the shape half covers is on the line either way. Anti-aliasing moves each
    bound by under a pixel, which is why the caller tolerates one.
    """
    def coverage(at):
        """The pixel as it would composite, so alpha and colour are comparable."""
        alpha = rgba[at + 3]
        return (*(rgba[at + c] * alpha // 255 for c in range(3)), alpha)

    ground = coverage(0)
    xs, ys = [], []
    for y in range(height):
        row = y * width
        for x in range(width):
            pixel = coverage((row + x) * 4)
            if max(abs(pixel[c] - ground[c]) for c in range(4)) > 128:
                xs.append(x)
                ys.append(y)
    assert xs, "found no mark in this icon - it is a blank rectangle"
    return min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1


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


def _rasters() -> list[tuple[str, bytes, float]]:
    """Each committed raster, with the margin its surface was drawn at."""
    out = [("apple-icon.png", APPLE.read_bytes(), _js_number("APPLE_MARGIN"))]
    out += [(f"favicon.ico entry {i}", payload, _js_number("FAVICON_MARGIN"))
            for i, payload in enumerate(_ico_pngs(FAVICON.read_bytes()))]
    return out


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
        "marks. Run `npm run icons` in web/.")


def test_the_favicon_is_placed_where_the_generator_says():
    """
    icon.svg's own transform, against the placement recomputed from the
    generator's constants.

    Without this, editing FAVICON_MARGIN and forgetting to run the script
    leaves a committed SVG that still looks entirely reasonable - and the
    raster check below would pass too, since the rasters are made from that
    same stale SVG. Everything downstream agrees; it just agrees on the old
    number.
    """
    scale, x, y = _layout(_js_number("FAVICON_MARGIN"))
    icon = ICON.read_text(encoding="utf-8")

    found = re.search(r"translate\(([\d.]+) ([\d.]+)\) scale\(([\d.]+)\)", icon)
    assert found, "app/icon.svg no longer places the mark with translate+scale"

    # Written to four decimal places, so a rounding difference is not a
    # failure; a changed margin moves these by whole units.
    for label, want, got in zip(("x", "y", "scale"),
                                (x, y, scale),
                                (float(n) for n in found.groups())):
        assert abs(want - got) < 0.01, (
            f"app/icon.svg places the mark at {label}={got}, but the "
            f"generator's constants give {want:.4f}. Run `npm run icons`.")

    assert icon.count("scale(") == 1, (
        "app/icon.svg should apply exactly one uniform scale to the mark; a "
        "second transform can distort it without anything else noticing")


def test_the_favicon_carries_both_mark_colours():
    """
    The tab icon is transparent, so it is drawn on the BROWSER's chrome, and
    this site has no light mode whose colours would apply. One fill is
    therefore wrong half the time - a near-black mark disappears into a dark
    tab strip, a white one into a light one.

    The media query inside the SVG is the only mechanism that still adapts
    after the file is served, so this checks it is actually there and that
    both colours came from lib/marks.ts rather than being typed in.
    """
    source = MARKS.read_text(encoding="utf-8")
    on_light = _ts_const(source, "MARK_ON_LIGHT")
    on_dark = _ts_const(source, "MARK_ON_DARK")
    icon = ICON.read_text(encoding="utf-8")

    assert "<rect" not in icon, (
        "app/icon.svg has a background rectangle again. It is meant to be "
        "transparent - a tab icon sits on the browser's chrome, and a tile "
        "of our own black is a dark square on a light toolbar.")

    default = re.search(r"^\s*path\s*\{\s*fill:\s*(#[0-9a-fA-F]{6})",
                        icon, re.M)
    assert default and default.group(1).lower() == on_light.lower(), (
        f"app/icon.svg's default fill should be MARK_ON_LIGHT ({on_light}). "
        f"It is what a renderer falls back to when it evaluates no media "
        f"queries - which is exactly what happens when this SVG is flattened "
        f"into favicon.ico.")

    dark = re.search(
        r"prefers-color-scheme:\s*dark\s*\)\s*\{\s*path\s*\{\s*fill:\s*"
        r"(#[0-9a-fA-F]{6})", icon)
    assert dark and dark.group(1).lower() == on_dark.lower(), (
        f"app/icon.svg no longer switches to MARK_ON_DARK ({on_dark}) under "
        f"`prefers-color-scheme: dark`. Without that rule the mark is "
        f"near-black on a dark tab strip, which is invisible.")


def test_each_icon_sits_on_the_ground_its_surface_needs():
    """
    Transparent for the tab, opaque for the home screen, and the two are not
    interchangeable.

    iOS composites a transparent home-screen icon onto a ground it chooses,
    so Apple's guidance is an opaque full-bleed tile; the colour has to be
    carried in lib/marks.ts because nothing outside the CSS pipeline can read
    `--background`, which is how the rasters drifted to pure black once
    before.
    """
    token = _ts_const(MARKS.read_text(encoding="utf-8"), "BRAND_BACKGROUND")

    css = re.search(r"--background:\s*(#[0-9a-fA-F]{6})",
                    GLOBALS.read_text(encoding="utf-8"))
    assert css, "--background is missing from globals.css"
    assert token.lower() == css.group(1).lower(), (
        f"BRAND_BACKGROUND is {token} but globals.css --background is "
        f"{css.group(1)} - the iOS tile no longer matches the page it opens")

    opaque = tuple(int(token[i:i + 2], 16) for i in (1, 3, 5)) + (255,)
    _, _, apple = _png_rgba(APPLE.read_bytes())
    assert tuple(apple[0:4]) == opaque, (
        f"apple-icon.png's ground is rgba{tuple(apple[0:4])}, not {token} at "
        f"full opacity. iOS composites any transparency onto a ground of its "
        f"own choosing. Run `npm run icons`.")

    for i, payload in enumerate(_ico_pngs(FAVICON.read_bytes())):
        _, _, rgba = _png_rgba(payload)
        assert rgba[3] == 0, (
            f"favicon.ico entry {i} has an opaque corner (alpha {rgba[3]}). "
            f"The tab icon is meant to be the mark alone, on whatever the "
            f"browser's toolbar happens to be.")


def test_the_rasters_are_the_same_drawing_as_the_vector():
    """
    Each raster places the mark where its own surface's margin says.

    This is the check that was missing when the rasters drifted, and it was
    missing for a reason worth writing down: the colour assertion above reads
    like a check that they were generated from the same source, and is not
    one. Both rasters were made from the brand kit's square PNG, whose
    padding is tighter, and every assertion in this file passed.

    Predicted from LOGOMARK_VIEWBOX through the generator's own layout rather
    than from hardcoded pixel counts, so retuning a margin moves the
    expectation with it instead of failing.

    The 16px ICO entry is along for the ride. A mark 12% too wide at that
    size lands on the same integer pixels as a correct one - the bug this
    test exists for is literally invisible there - so the 32px entry and
    apple-icon.png are what carry it. Checked anyway, because a gross error
    would still show.
    """
    canvas = _js_number("CANVAS")
    vb_w, vb_h = _logomark_viewbox()

    # One pixel, flat, NOT scaled to the icon. Each measured bound is an
    # integer taken from a thresholded anti-aliased edge, so it carries up to
    # half a pixel; width and height combine two bounds, so a pixel is the
    # quantisation bound for all four numbers at every size.
    #
    # It was 2px, which is where the first version of this test went wrong:
    # at 180px that caught the apple-icon error (4.4px) and read like a
    # working test, while the SAME bug in favicon.ico measures 1.368px at
    # 32px and sailed through. The suite passed on a broken ICO. A tolerance
    # in pixels has to be a statement about measurement noise, which does not
    # grow with the icon, rather than a fraction of the artwork.
    TOLERANCE = 1

    for name, data, margin in _rasters():
        width, height, rgba = _png_rgba(data)
        assert width == height, f"{name} is not square ({width}x{height})"

        scale, x, y = _layout(margin)
        px = width / canvas          # canvas units to pixels at this size
        expected = (x * px, y * px, vb_w * scale * px, vb_h * scale * px)
        actual = _mark_bounds(width, height, rgba)

        for label, want, got in zip(("left", "top", "width", "height"),
                                    expected, actual):
            assert abs(want - got) <= TOLERANCE, (
                f"{name} ({width}px) draws the mark's {label} at {got}px, but "
                f"the generator's layout puts it at {want:.1f}px. This raster "
                f"is not what build-icons.mjs would write.\n"
                f"    cd web && npm run icons")


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
        width, height, _ = _png_rgba(payload)
        assert (entry[0] or 256, entry[1] or 256) == (width, height), (
            f"favicon.ico entry {i} is declared "
            f"{entry[0] or 256}x{entry[1] or 256} but holds a "
            f"{width}x{height} PNG")
        declared.append(width)

    wanted = _js_number_array("ICO_SIZES")
    assert sorted(declared) == sorted(wanted), (
        f"favicon.ico holds {sorted(declared)} but build-icons.mjs generates "
        f"{sorted(wanted)} - the committed file is not this script's output. "
        f"Run `npm run icons` in web/.")

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
