"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { clearToken, getToken, UNAUTHORIZED_EVENT } from "@/lib/api";
import { useEffect, useState } from "react";
import { useInfo } from "./AuthGate";
import { cx } from "./ui";

const LINKS = [{ href: "/upload", label: "Upload" }, { href: "/documents", label: "Documents" }, { href: "/search", label: "Search" }];

export function Nav() {
  const path = usePathname();
  const info = useInfo();
  const [signedIn, setSignedIn] = useState(false);
  useEffect(() => setSignedIn(!!getToken()), [path]);
  const demo = info?.llm_provider === "demo";

  return (
    <header className="sticky top-0 z-20 border-b border-slate-200 bg-white/95 backdrop-blur">
      <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-x-6 gap-y-1 px-4 py-2">
        <Link href="/documents" className="text-base font-semibold text-slate-900">Document Intelligence</Link>
        <nav className="flex gap-1" aria-label="Main">
          {LINKS.map((l) => (
            <Link key={l.href} href={l.href}
              className={cx("rounded-md px-3 py-1.5 text-sm font-medium", path.startsWith(l.href) ? "bg-slate-900 text-white" : "text-slate-600 hover:bg-slate-100")}>
              {l.label}
            </Link>
          ))}
        </nav>
        <div className="ml-auto flex items-center gap-2 text-xs">
          {info && (
            <span className={cx("rounded-full px-2.5 py-1", demo ? "bg-amber-100 text-amber-900" : "bg-slate-100 text-slate-600")}
              title={demo ? "Extraction uses simple rules, not an AI model" : "Model used for extraction"}>
              {demo ? "DEMO extractor (not AI)" : info.llm_model}
            </span>
          )}
          {signedIn && (
            <button className="btn px-2 py-1 text-xs" onClick={() => { clearToken(); window.dispatchEvent(new Event(UNAUTHORIZED_EVENT)); }}>Sign out</button>
          )}
        </div>
      </div>
    </header>
  );
}
