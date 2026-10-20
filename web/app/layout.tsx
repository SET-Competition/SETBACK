import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import Link from "next/link";
import { Wordmark } from "./brand";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

const DESCRIPTION =
  "Crypto trading competition ranked on a flow-adjusted Sharpe ratio.";

// Each route sets its own title; this is the fallback and the site-wide
// description.
//
// metadataBase is required for the social card to work. Without it Next
// cannot turn the relative opengraph-image path into the absolute URL that
// Slack, Discord and X all demand, and the card silently does not render -
// there is no error, the link just comes out bare. It is read from the
// environment so a preview deployment advertises its own images rather than
// production's; VERCEL_URL is set by Vercel on every deployment and carries
// no scheme, hence the template.
export const metadata: Metadata = {
  metadataBase: new URL(
    process.env.NEXT_PUBLIC_SITE_URL ??
      (process.env.VERCEL_URL
        ? `https://${process.env.VERCEL_URL}`
        : "https://app.set.trade"),
  ),
  title: "SET",
  description: DESCRIPTION,
  openGraph: {
    title: "SET",
    description: DESCRIPTION,
    siteName: "SET",
    type: "website",
  },
  twitter: {
    card: "summary_large_image",
    title: "SET",
    description: DESCRIPTION,
  },
};

function Nav() {
  return (
    <nav className="border-b border-white/10">
      <div className="mx-auto flex w-full max-w-5xl items-center gap-5 px-4 py-3.5 sm:px-6">
        <Link
          href="/leaderboard"
          aria-label="SET - home"
          className="text-white transition-opacity hover:opacity-70"
        >
          {/* Height-constrained, width auto: the lockup is 4.25:1, so sizing
              it by width would leave it a different height on every
              breakpoint and misalign it with the links beside it. */}
          <Wordmark className="h-[18px] w-auto" />
        </Link>
        <div className="ml-auto flex items-center gap-4 text-sm">
          <Link href="/leaderboard" className="text-white/50 transition-colors hover:text-white">
            Leaderboard
          </Link>
          <Link
            href="/register"
            className="rounded-md bg-white/10 px-3 py-1.5 font-medium text-white transition-colors hover:bg-white/15"
          >
            Register
          </Link>
        </div>
      </div>
    </nav>
  );
}

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="flex min-h-full flex-col">
        <Nav />
        {children}
      </body>
    </html>
  );
}
