import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
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
  title: "OVAEL — Learn what is holding you back",
  description:
    "A source-grounded teaching companion that finds and repairs learning gaps.",
  openGraph: {
    title: "OVAEL",
    description: "Learn what is holding you back.",
    images: [{ url: "/og.png", width: 1200, height: 630, alt: "OVAEL — Learn what is holding you back." }],
  },
  twitter: {
    card: "summary_large_image",
    title: "OVAEL",
    description: "Learn what is holding you back.",
    images: ["/og.png"],
  },
  icons: {
    icon: "/favicon.svg",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body
        className={`${geistSans.variable} ${geistMono.variable} antialiased`}
      >
        {children}
      </body>
    </html>
  );
}
