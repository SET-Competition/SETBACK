/**
 * The sentences that describe the competition, in one place.
 *
 * These were written out four times - the root metadata, the leaderboard
 * route's own metadata, the social card's alt text and the card's pixels -
 * in four slightly different forms. Nothing made them agree, and one of the
 * four is a PNG: change the pitch and the shared card keeps advertising the
 * old one, silently, because nobody greps an image.
 *
 * Same reasoning as tests/test_signup_form.py and
 * tests/test_leaderboard_page.py, which exist to stop a value written down
 * twice from drifting. tests/test_brand_copy.py pins these.
 */

/** The full sentence. Used for <meta name="description"> and og:description. */
export const DESCRIPTION =
  "Crypto trading competition ranked on a flow-adjusted Sharpe ratio.";

/**
 * The short form, for the social card's artwork.
 *
 * Deliberately not DESCRIPTION: the card already shows the SET lockup, so
 * repeating "Crypto trading competition" underneath it is the logo's job
 * done twice. Kept here rather than inline so the card's alt text can be
 * derived from it instead of being a fifth variant.
 */
export const TAGLINE = "Ranked on a flow-adjusted Sharpe ratio";

/** What a screen reader hears in place of the social card. */
export const CARD_ALT = `SET - ${TAGLINE.toLowerCase()}`;
