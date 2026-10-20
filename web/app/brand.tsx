import {
  LOGOMARK_PATHS,
  LOGOMARK_VIEWBOX,
  WORDMARK_PATHS,
  WORDMARK_VIEWBOX,
} from "@/lib/marks";

/**
 * The SET marks, inlined rather than served as files.
 *
 * Inline for one reason: `fill="currentColor"`. The brand kit ships black and
 * white variants that are byte-identical apart from that attribute, so an
 * <img> would mean shipping both and picking one per surface - and picking
 * wrong wherever a background later changes. Taking the colour from CSS means
 * there is one copy of each mark and it is always legible against whatever it
 * sits on.
 *
 * Inlining also removes two render-blocking requests for ~6KB of geometry,
 * and lets the marks inherit opacity and hover transitions from their parent
 * the way text does.
 *
 * The geometry comes from lib/marks.ts, which app/icon.svg and the social
 * card are pinned to as well.
 */

type MarkProps = {
  className?: string;
  /**
   * Accessible name. Pass `null` where the mark is decorative - sat beside
   * the word SET in text, or inside an element that already carries its own
   * label - so a screen reader does not announce the brand twice.
   *
   * A prop rather than a hardcoded value because the right answer differs per
   * placement, and the first use that needed the other one would otherwise
   * have to edit the component.
   */
  label?: string | null;
};

/**
 * Width and height are set alongside the viewBox deliberately. Without them
 * an inline SVG with only a ratio falls back to 300px wide until the
 * stylesheet applies - a four-times-oversized logo in the window before CSS
 * lands. Next emits its CSS render-blocking so this is not reachable today,
 * but the attributes cost nothing and the failure is ugly.
 */
function a11y(label: string | null | undefined, fallback: string) {
  const name = label === undefined ? fallback : label;
  return name === null
    ? { "aria-hidden": true as const }
    : { role: "img" as const, "aria-label": name };
}

/** Full lockup: the mark followed by the SET wordmark. Wide - 2552x600. */
export function Wordmark({ className, label }: MarkProps) {
  const [, , w, h] = WORDMARK_VIEWBOX.split(" ");
  return (
    <svg
      viewBox={WORDMARK_VIEWBOX}
      width={w}
      height={h}
      className={className}
      fill="currentColor"
      {...a11y(label, "SET")}
    >
      {WORDMARK_PATHS.map((d) => (
        <path key={d.slice(0, 32)} d={d} />
      ))}
    </svg>
  );
}

/**
 * The mark alone, for square surfaces and anywhere the lockup would be too
 * wide to read. Decorative by default: it is almost always sat beside the
 * word SET, and a screen reader announcing the logo twice is noise.
 */
export function Logomark({ className, label = null }: MarkProps) {
  const [, , w, h] = LOGOMARK_VIEWBOX.split(" ");
  return (
    <svg
      viewBox={LOGOMARK_VIEWBOX}
      width={w}
      height={h}
      className={className}
      fill="currentColor"
      {...a11y(label, "SET")}
    >
      {LOGOMARK_PATHS.map((d) => (
        <path key={d.slice(0, 32)} d={d} />
      ))}
    </svg>
  );
}
