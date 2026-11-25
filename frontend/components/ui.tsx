import Link from "next/link";
import type { ReactNode } from "react";
import { ApiError } from "@/lib/api";
import { statusLabel } from "@/lib/format";

export const cx = (...parts: (string | false | null | undefined)[]): string => parts.filter(Boolean).join(" ");

const STATUS_STYLE: Record<string, string> = {
  queued: "bg-slate-100 text-slate-700 ring-slate-300",
  processing: "bg-blue-50 text-blue-700 ring-blue-200",
  completed: "bg-green-50 text-green-700 ring-green-200",
  failed: "bg-red-50 text-red-700 ring-red-200",
  needs_review: "bg-amber-50 text-amber-800 ring-amber-300",
};

export function StatusBadge({ status }: { status: string }) {
  return (
    <span className={cx("inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium capitalize ring-1 ring-inset",
      STATUS_STYLE[status] ?? STATUS_STYLE.queued)}>
      {status === "processing" && <Spinner className="h-3 w-3" />}
      {statusLabel(status)}
    </span>
  );
}

export function Pill({ children, tone = "slate" }: { children: ReactNode; tone?: "slate" | "red" | "amber" | "green" | "blue" }) {
  const t = { slate: "bg-slate-100 text-slate-700", red: "bg-red-50 text-red-700", amber: "bg-amber-50 text-amber-800",
    green: "bg-green-50 text-green-700", blue: "bg-blue-50 text-blue-700" }[tone];
  return <span className={cx("inline-flex items-center rounded px-1.5 py-0.5 text-xs font-medium", t)}>{children}</span>;
}

export function Spinner({ className = "h-4 w-4" }: { className?: string }) {
  return (
    <svg className={cx("animate-spin", className)} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle cx="12" cy="12" r="10" stroke="currentColor" strokeOpacity="0.25" strokeWidth="4" />
      <path d="M22 12a10 10 0 0 0-10-10" stroke="currentColor" strokeWidth="4" strokeLinecap="round" />
    </svg>
  );
}

export function ProgressBar({ value, label, tone = "blue" }: { value: number; label?: string; tone?: "blue" | "green" | "red" }) {
  const v = Math.max(0, Math.min(100, Math.round(value)));
  const color = { blue: "bg-blue-600", green: "bg-green-600", red: "bg-red-500" }[tone];
  return (
    <div className="flex items-center gap-2" role="progressbar" aria-valuenow={v} aria-valuemin={0} aria-valuemax={100} aria-label={label ?? "Progress"}>
      <div className="h-2 min-w-[4rem] flex-1 overflow-hidden rounded-full bg-slate-200">
        <div className={cx("h-full rounded-full transition-all duration-500", color)} style={{ width: `${v}%` }} />
      </div>
      <span className="w-9 text-right text-xs tabular-nums text-slate-600">{v}%</span>
    </div>
  );
}

export function LoadingBlock({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-2 py-12 text-sm text-slate-500" role="status">
      <Spinner className="h-5 w-5" /> {label}
    </div>
  );
}

export function ErrorState({ error, onRetry, title = "Something went wrong" }: { error: unknown; onRetry?: () => void; title?: string }) {
  const message = error instanceof ApiError ? error.message : error instanceof Error ? error.message : String(error);
  return (
    <div className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-800" role="alert">
      <p className="font-medium">{title}</p>
      <p className="mt-1">{message}</p>
      {error instanceof ApiError && error.requestId && <p className="mt-1 text-xs text-red-600">Request id: {error.requestId}</p>}
      {onRetry && <button className="btn mt-3" onClick={onRetry}>Try again</button>}
    </div>
  );
}

export function EmptyState({ title, hint, action }: { title: string; hint?: string; action?: { href: string; label: string } | { onClick: () => void; label: string } }) {
  return (
    <div className="rounded-lg border border-dashed border-slate-300 bg-white px-4 py-12 text-center">
      <p className="font-medium text-slate-800">{title}</p>
      {hint && <p className="mt-1 text-sm text-slate-500">{hint}</p>}
      {action && ("href" in action
        ? <Link className="btn btn-primary mt-4" href={action.href}>{action.label}</Link>
        : <button className="btn mt-4" onClick={action.onClick}>{action.label}</button>)}
    </div>
  );
}

export function Pagination({ page, pages, onPage }: { page: number; pages: number; onPage: (p: number) => void }) {
  if (pages <= 1) return null;
  return (
    <nav className="mt-4 flex items-center justify-between text-sm" aria-label="Pagination">
      <button className="btn" disabled={page <= 1} onClick={() => onPage(page - 1)}>‹ Previous</button>
      <span className="text-slate-600">Page {page} of {pages}</span>
      <button className="btn" disabled={page >= pages} onClick={() => onPage(page + 1)}>Next ›</button>
    </nav>
  );
}
