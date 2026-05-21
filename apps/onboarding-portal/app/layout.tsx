import type { Metadata } from "next";
import "./globals.css";
import { Navigation } from "@/components/Navigation";
import { Footer } from "@/components/Footer";

export const metadata: Metadata = {
  title: "Jaringan Dagang Network Dashboard",
  description:
    "Network visualization and management dashboard for Indonesia's Beckn open commerce network",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className="dark">
      <body className="min-h-screen bg-surface-900 text-slate-200 antialiased">
        <Navigation />
        <main className="min-h-[calc(100vh-64px-48px)]">{children}</main>
        <Footer />
      </body>
    </html>
  );
}
