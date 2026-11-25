"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { Icon, type IconName } from "@/components/icons";
import { cx } from "@/components/ui";
import { api } from "@/lib/api";

type Group = "extract" | "review" | "find";
interface Tool { href: string; title: string; text: string; icon: IconName; group: Group; tint: string; status?: string }

const TOOLS: Tool[] = [
  { href: "/upload", title: "Extract invoice data", text: "Upload PDFs, scans, photos, Excel or CSV and get the fields and line items back.", icon: "upload", group: "extract", tint: "bg-brand-50 text-brand-600" },
  { href: "/documents", title: "All documents", text: "Everything you uploaded, with live progress and filters.", icon: "folder", group: "review", tint: "bg-sky-50 text-sky-600" },
  { href: "/documents?status=needs_review", title: "Needs review", text: "Results the checks were not sure about. Open one to compare and correct it.", icon: "review", group: "review", tint: "bg-amber-50 text-amber-600", status: "needs_review" },
  { href: "/documents?status=completed", title: "Completed", text: "Invoices that passed every validation rule.", icon: "check", group: "review", tint: "bg-green-50 text-green-600", status: "completed" },
  { href: "/documents?status=failed", title: "Failed", text: "Files that could not be read. See why, then reprocess or delete.", icon: "alert", group: "review", tint: "bg-red-50 text-red-600", status: "failed" },
  { href: "/search", title: "Search documents", text: "Find invoices by words, supplier, number or amount: keyword, semantic or both.", icon: "search", group: "find", tint: "bg-violet-50 text-violet-600" },
];

const TABS: { id: "all" | Group; label: string }[] = [
  { id: "all", label: "All" }, { id: "extract", label: "Extract" }, { id: "review", label: "Review" }, { id: "find", label: "Find" },
];

export default function Home() {
  const [tab, setTab] = useState<"all" | Group>("all");
  const [counts, setCounts] = useState<Record<string, number>>({});

  useEffect(() => {
    let live = true;
    const statuses = TOOLS.flatMap((t) => (t.status ? [t.status] : []));
    void Promise.allSettled(statuses.map((s) => api.listDocuments({ status: s, page: 1, page_size: 1 }))).then((rs) => {
      if (!live) return;
      const next: Record<string, number> = {};
      rs.forEach((r, i) => { if (r.status === "fulfilled") next[statuses[i]] = r.value.total; });
      setCounts(next);
    });
    return () => { live = false; };
  }, []);

  return (
    <div className="-mt-5">
      <section className="px-2 pb-8 pt-12 text-center">
        <h1 className="mx-auto max-w-3xl text-4xl font-extrabold leading-tight tracking-tight text-slate-900 sm:text-5xl">
          Every step you need to turn invoices into data
        </h1>
        <p className="mx-auto mt-4 max-w-2xl text-lg text-slate-600">
          Read, check and search your invoices in one place. Every value shows where in the document it came from, and anything doubtful is sent to a person.
        </p>
        <Link href="/upload" className="btn btn-primary btn-xl mt-8 inline-flex">
          Select invoice files <Icon name="arrow" className="h-6 w-6" />
        </Link>
      </section>

      <div className="mb-6 flex flex-wrap justify-center gap-2" role="tablist" aria-label="Filter tools">
        {TABS.map((t) => (
          <button key={t.id} role="tab" aria-selected={tab === t.id} onClick={() => setTab(t.id)}
            className={cx("pill-tab", tab === t.id && "pill-tab-on")}>{t.label}</button>
        ))}
      </div>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {TOOLS.filter((t) => tab === "all" || t.group === tab).map((t) => (
          <Link key={t.href} href={t.href}
            className="group relative flex flex-col rounded-2xl border border-slate-200 bg-white p-6 shadow-card transition hover:-translate-y-0.5 hover:shadow-lift">
            <span className={cx("grid h-14 w-14 place-items-center rounded-2xl", t.tint)}><Icon name={t.icon} className="h-8 w-8" /></span>
            <h2 className="mt-4 text-lg font-bold text-slate-900">{t.title}</h2>
            <p className="mt-1.5 text-sm leading-relaxed text-slate-600">{t.text}</p>
            {t.status && counts[t.status] !== undefined && (
              <span className="absolute right-4 top-4 rounded-full bg-slate-100 px-2.5 py-0.5 text-xs font-semibold tabular-nums text-slate-700"
                aria-label={`${counts[t.status]} documents`}>{counts[t.status]}</span>
            )}
          </Link>
        ))}
      </div>
    </div>
  );
}
