import type { Metadata, Viewport } from "next";
import "./globals.css";
import { AuthGate } from "@/components/AuthGate";
import { Nav } from "@/components/Nav";

export const metadata: Metadata = { title: "Document Intelligence", description: "Invoice extraction, validation and search" };
export const viewport: Viewport = { width: "device-width", initialScale: 1 };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <AuthGate>
          <Nav />
          <main className="mx-auto max-w-7xl px-4 py-5">{children}</main>
        </AuthGate>
      </body>
    </html>
  );
}
