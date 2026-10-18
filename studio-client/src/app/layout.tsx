import type { Metadata } from "next";
import {
  Archivo,
  DM_Mono,
  DM_Sans,
  Figtree,
  Geist,
  Hanken_Grotesk,
  IBM_Plex_Sans,
  Instrument_Sans,
  Inter,
  Lexend,
  Manrope,
  Outfit,
  Plus_Jakarta_Sans,
  Sora,
  Space_Grotesk,
  Work_Sans,
} from "next/font/google";
import type { ReactNode } from "react";

import { CompanionShell } from "@/components/follow-me/companion-shell";
import { Sidebar } from "@/components/sidebar/sidebar";

import { Providers } from "./providers";
import "./globals.css";

export const metadata: Metadata = {
  title: "AI Factory Studio",
  description: "AI Factory Studio's local view over the indexed knowledge base and its chat interface",
};

// Self-hosted at build time; --font-app is what globals.css's body font stack
// reads, --font-mono-app is what its font-mono mapping reads (never the bare
// `--font-mono` name — see globals.css). Space Grotesk sets everything a
// person or the product says; DM Mono sets identifiers and machine output.
const spaceGrotesk = Space_Grotesk({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-grotesk",
});
const dmMono = DM_Mono({
  subsets: ["latin"],
  weight: ["400", "500"],
  variable: "--font-mono-app",
});

// The other faces the /settings Type picker offers, every one carrying real medium and bold
// weights. Self-hosted like the default but not preloaded: a face is fetched only once the
// stamped choice actually uses it, so the menu costs nothing to whoever keeps the default.
const inter = Inter({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-inter",
  preload: false,
});
const manrope = Manrope({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-manrope",
  preload: false,
});
const plexSans = IBM_Plex_Sans({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-plex",
  preload: false,
});
const outfit = Outfit({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-outfit",
  preload: false,
});
const dmSans = DM_Sans({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-dm-sans",
  preload: false,
});
const geist = Geist({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-geist",
  preload: false,
});
const sora = Sora({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-sora",
  preload: false,
});
const jakarta = Plus_Jakarta_Sans({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-jakarta",
  preload: false,
});
const workSans = Work_Sans({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-work-sans",
  preload: false,
});
const figtree = Figtree({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-figtree",
  preload: false,
});
const hanken = Hanken_Grotesk({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-hanken",
  preload: false,
});
const instrument = Instrument_Sans({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-instrument",
  preload: false,
});
const lexend = Lexend({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-lexend",
  preload: false,
});
const archivo = Archivo({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-archivo",
  preload: false,
});

// Stamps the persisted theme on <html> BEFORE first paint, so a reload never flashes
// the default skin. Runs inline and synchronously on purpose; the key and the theme
// names must agree with the Appearance section on /settings.
const THEME_STAMP = `try {
  var t = localStorage.getItem("studio-theme");
  if (t === "factory" || t === "graphite") document.documentElement.dataset.theme = t;
  var f = localStorage.getItem("studio-font");
  if (["inter","manrope","plex","outfit","dm-sans","geist","sora","jakarta","work-sans","figtree","hanken","instrument","lexend","archivo"].indexOf(f) !== -1) document.documentElement.dataset.font = f;
} catch (e) {}`;

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    // suppressHydrationWarning: the pre-paint script stamps data-theme before React
    // hydrates, so the attribute legitimately differs from the server render.
    <html
      lang="en"
      className={`h-full ${spaceGrotesk.variable} ${dmMono.variable} ${inter.variable} ${manrope.variable} ${plexSans.variable} ${outfit.variable} ${dmSans.variable} ${geist.variable} ${sora.variable} ${jakarta.variable} ${workSans.variable} ${figtree.variable} ${hanken.variable} ${instrument.variable} ${lexend.variable} ${archivo.variable}`}
      suppressHydrationWarning
    >
      {/* overflow-hidden: the app fills the window and only its own panes scroll, so the
          document never grows a second scrollbar beside a pane's. */}
      <body className="h-full overflow-hidden bg-ground text-label-1">
        {/* biome-ignore lint/security/noDangerouslySetInnerHtml: the pre-paint theme stamp is a build-time constant */}
        <script dangerouslySetInnerHTML={{ __html: THEME_STAMP }} />
        <Providers>
          <div className="flex h-full">
            <Sidebar />
            <CompanionShell>{children}</CompanionShell>
          </div>
        </Providers>
      </body>
    </html>
  );
}
