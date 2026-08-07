import type { Metadata, Viewport } from "next";
import { Geist, Geist_Mono } from "next/font/google";

import { Providers } from "@/app/providers";
import { Toaster } from "@/components/ui/sonner";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Picox — Insight dai video brevi",
  description:
    "Analizza Reels, TikTok e Shorts con AI multimodale ed estrai insight riutilizzabili.",
  manifest: "/manifest.json",
  applicationName: "Picox",
  icons: {
    icon: "/icon.svg",
  },
};

export const viewport: Viewport = {
  themeColor: "#0a0a0a",
  colorScheme: "dark",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    // `dark` fisso sull'elemento radice: il dark mode non è una preferenza ma
    // il design del prodotto, quindi non passa da next-themes né dal sistema.
    <html
      lang="it"
      className={`dark ${geistSans.variable} ${geistMono.variable} h-full antialiased`}
      suppressHydrationWarning
    >
      <body className="bg-background text-foreground min-h-full flex flex-col">
        <Providers>
          {children}
          <Toaster position="bottom-right" richColors closeButton />
        </Providers>
      </body>
    </html>
  );
}
