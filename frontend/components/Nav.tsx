"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { clearToken, getToken, UNAUTHORIZED_EVENT } from "@/lib/api";
import { useEffect, useState } from "react";
import { useInfo } from "./AuthGate";
import { Icon } from "./icons";
import { cx } from "./ui";

const LINKS = [{ href: "/upload", label: "Upload" }, { href: "/documents", label: "Documents" }, { href: "/search", label: "Search" }];

export function Nav() {
  const path = usePathname();
  const info = useInfo();
  const [signedIn, setSignedIn] = useState(false);
  useEffect(() => setSignedIn(!!getToken()), [path]);
  const demo = info?.llm_provider === "demo";

  return (
    <header className="sticky top-0 z-20 border-b border-slate-200 bg-white shadow-sm">
      <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-x-8 gap-y-1 px-4 py-3">
        <Link href="/" className="flex items-center gap-2.5 text-lg font-bold tracking-tight text-slate-900" aria-label="Document Intelligence, home">
          <span className="grid h-8 w-8 place-items-center rounded-lg bg-brand-600 text-white"><Icon name="file" className="h-5 w-5" /></span>
          <span>Document <span className="text-brand-600">Intelligence</span></span>
        </Link>
        <nav className="flex gap-1" aria-label="Main">
          {LINKS.map((l) => (
            <Link key={l.href} href={l.href} aria-current={path.startsWith(l.href) ? "page" : undefined}
              className={cx("rounded-lg px-3 py-1.5 text-sm font-semibold",
                path.startsWith(l.href) ? "bg-brand-50 text-brand-700" : "text-slate-700 hover:bg-slate-100")}>
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
            <button className="btn px-3 py-1.5 text-xs" onClick={() => { clearToken(); window.dispatchEvent(new Event(UNAUTHORIZED_EVENT)); }}>Sign out</button>
          )}
        </div>
      </div>
    </header>
  );
}
