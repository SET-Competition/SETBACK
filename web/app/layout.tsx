import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import Link from "next/link";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

// Each route sets its own title; this is the fallback and the site-wide
// description.
export const metadata: Metadata = {
  title: "SET",
  description:
    "Crypto trading competition ranked on a flow-adjusted Sharpe ratio.",
};

function Nav() {
  return (
    <nav className="border-b border-white/10">
      <div className="mx-auto flex w-full max-w-5xl items-center gap-5 px-4 py-3.5 sm:px-6">
        <Link
          href="/leaderboard"
          className="font-mono text-sm font-semibold tracking-widest text-white"
        >
          SET
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
