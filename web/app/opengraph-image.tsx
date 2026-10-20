import { ImageResponse } from "next/og";

import { CARD_ALT, TAGLINE } from "@/lib/copy";
import {
  BRAND_BACKGROUND,
  WORDMARK_PATHS,
  WORDMARK_VIEWBOX,
} from "@/lib/marks";

/**
 * The card platforms render when a SET link is shared.
 *
 * Generated rather than committed as a PNG so the wording and the mark track
 * the site: a flat image is a second copy of the pitch that nobody remembers
 * to update, because nobody greps an image.
 *
 * The mark is passed as a data URI rather than a path. Satori renders an
 * <img>, not arbitrary SVG children, and it has no access to the filesystem
 * or to public/ - a relative src is dropped SILENTLY and the card ships with
 * a hole in it. Built from lib/marks.ts at module scope rather than pasted,
 * so restyling the logo cannot leave the share card showing the old one.
 */

export const alt = CARD_ALT;
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

const [, , VB_WIDTH, VB_HEIGHT] = WORDMARK_VIEWBOX.split(" ").map(Number);

const WORDMARK_SVG =
  `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${WORDMARK_VIEWBOX}" fill="#ffffff">` +
  WORDMARK_PATHS.map((d) => `<path d="${d}"/>`).join("") +
  `</svg>`;

const WORDMARK = `data:image/svg+xml;base64,${Buffer.from(WORDMARK_SVG).toString("base64")}`;

// Height follows from the width so the lockup cannot be stretched if the
// viewBox ever changes shape. Satori needs both stated explicitly.
const RENDER_WIDTH = 620;
const RENDER_HEIGHT = Math.round(RENDER_WIDTH * (VB_HEIGHT / VB_WIDTH));

export default function Image() {
  return new ImageResponse(
    (
      <div
        style={{
          width: "100%",
          height: "100%",
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          background: BRAND_BACKGROUND,
        }}
      >
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src={WORDMARK}
          width={RENDER_WIDTH}
          height={RENDER_HEIGHT}
          alt=""
        />
        <div
          style={{
            display: "flex",
            marginTop: 48,
            fontSize: 30,
            color: "rgba(237,237,237,0.55)",
            letterSpacing: "-0.01em",
          }}
        >
          {TAGLINE}
        </div>
      </div>
    ),
    size,
  );
}
