"use client";
import Link from "next/link";
import { useCallback, useRef, useState } from "react";
import { useInfo } from "@/components/AuthGate";
import { Icon, fileIcon } from "@/components/icons";
import { ProgressBar, Spinner, cx } from "@/components/ui";
import { ApiError, uploadFile } from "@/lib/api";
import { formatBytes } from "@/lib/format";
import { checkFile, runPool } from "@/lib/upload";

type ItemState = "invalid" | "ready" | "waiting" | "uploading" | "done" | "duplicate" | "error";
interface Item {
  id: number;
  file: File;
  state: ItemState;
  progress: number;
  message?: string;
  documentId?: string;
}

const CONCURRENCY = 3;
let nextId = 1;

function StateLine({ it }: { it: Item }) {
  const href = it.documentId ? `/documents/${it.documentId}` : "/documents";
  switch (it.state) {
    case "done": return <Link href={href} className="text-sm font-semibold text-green-700 hover:underline">✓ Uploaded</Link>;
    case "duplicate": return <Link href={href} className="text-sm font-semibold text-amber-700 hover:underline">Already uploaded</Link>;
    case "invalid": return <span className="text-sm font-semibold text-red-700">Not uploaded</span>;
    case "error": return <span className="text-sm font-semibold text-red-700">Failed</span>;
    case "waiting": return <span className="text-sm text-slate-500">Waiting…</span>;
    case "ready": return <span className="text-sm text-slate-500">Ready</span>;
    default: return null;
  }
}

export default function UploadPage() {
  const info = useInfo();
  const [items, setItems] = useState<Item[]>([]);
  const [over, setOver] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  const patch = useCallback((id: number, change: Partial<Item>) =>
    setItems((prev) => prev.map((it) => (it.id === id ? { ...it, ...change } : it))), []);

  const send = useCallback(async (it: Item) => {
    patch(it.id, { state: "uploading", progress: 0, message: undefined });
    try {
      const r = await uploadFile(it.file, (p) => patch(it.id, { progress: p }));
      patch(it.id, { state: "done", progress: 100, documentId: r.id ?? undefined });
    } catch (e) {
      if (e instanceof ApiError && e.code === "duplicate_file") {
        patch(it.id, { state: "duplicate", message: e.message, documentId: e.existingDocumentId ?? undefined });
      } else if (e instanceof ApiError && e.code === "rate_limited") {
        patch(it.id, { state: "error", message: "Too many uploads at once. Wait a minute, then press Retry." });
      } else {
        patch(it.id, { state: "error", message: e instanceof Error ? e.message : "Upload failed." });
      }
    }
  }, [patch]);

  // Choosing files only lists them (like a shopping basket); nothing is sent until the big button is pressed.
  const add = useCallback((files: FileList | File[]) => {
    const fresh: Item[] = Array.from(files).map((file) => {
      const problem = checkFile(file, info);
      return { id: nextId++, file, state: problem ? "invalid" : "ready", progress: 0, message: problem ?? undefined };
    });
    if (fresh.length) setItems((prev) => [...prev, ...fresh]);
  }, [info]);

  const start = () => {
    const ready = items.filter((i) => i.state === "ready");
    setItems((prev) => prev.map((i) => (i.state === "ready" ? { ...i, state: "waiting" } : i)));
    void runPool(ready, CONCURRENCY, send);
  };
  const retryFailed = () => void runPool(items.filter((i) => i.state === "error"), CONCURRENCY, send);
  const remove = (id: number) => setItems((prev) => prev.filter((i) => i.id !== id));

  const ready = items.filter((i) => i.state === "ready").length;
  const done = items.filter((i) => i.state === "done").length;
  const busy = items.some((i) => i.state === "uploading" || i.state === "waiting");
  const failed = items.filter((i) => i.state === "error").length;
  const readyBytes = items.filter((i) => i.state === "ready").reduce((n, i) => n + i.file.size, 0);
  const extensions = info?.extensions.join("  ") ?? ".pdf .jpg .png .tif .xlsx .xls .csv";
  const picker = (
    <input ref={input} type="file" multiple hidden data-testid="file-input"
      onChange={(e) => { if (e.target.files) add(e.target.files); e.target.value = ""; }} />
  );
  const dropProps = {
    onDragOver: (e: React.DragEvent) => { e.preventDefault(); setOver(true); },
    onDragLeave: () => setOver(false),
    onDrop: (e: React.DragEvent) => { e.preventDefault(); setOver(false); add(e.dataTransfer.files); },
  };

  if (items.length === 0) {
    return (
      <div className="mx-auto max-w-3xl pt-8 text-center" {...dropProps}>
        <h1 className="page-title sm:text-4xl">Extract invoice data</h1>
        <p className="mt-3 text-lg text-slate-600">Upload invoices, receipts, scans or spreadsheets. Several files at once are fine.</p>
        <div className={cx("mt-8 rounded-2xl border-2 border-dashed px-4 py-16 transition", over ? "border-brand-500 bg-brand-50" : "border-slate-300 bg-white")}>
          <button type="button" className="btn btn-primary btn-xl" onClick={() => input.current?.click()}>
            <Icon name="upload" className="h-6 w-6" /> Select invoice files
          </button>
          <p className="mt-4 text-sm text-slate-500">or drop files here</p>
          <p className="mt-6 text-xs text-slate-500">
            Allowed: <span className="font-mono">{extensions}</span> · up to {info?.max_file_size_mb ?? 200} MB each
          </p>
        </div>
        {picker}
      </div>
    );
  }

  return (
    <div className="grid gap-6 lg:grid-cols-[1fr_20rem]" {...dropProps}>
      <section aria-live="polite" className={cx("rounded-2xl border-2 border-dashed p-4 transition", over ? "border-brand-500 bg-brand-50" : "border-transparent")}>
        <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
          <h1 className="text-2xl font-bold tracking-tight">Your files <span className="text-slate-400">({items.length})</span></h1>
          <button className="btn" onClick={() => input.current?.click()}><Icon name="plus" className="h-4 w-4" /> Add more files</button>
        </div>
        <ul className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {items.map((it) => (
            <li key={it.id} className="card relative flex flex-col gap-2 p-3">
              {(it.state === "ready" || it.state === "invalid") && (
                <button className="absolute right-2 top-2 grid h-7 w-7 place-items-center rounded-full text-slate-400 hover:bg-slate-100 hover:text-slate-700"
                  onClick={() => remove(it.id)} aria-label={`Remove ${it.file.name}`}><Icon name="close" className="h-4 w-4" /></button>
              )}
              <div className="flex items-center gap-3 pr-6">
                <span className="grid h-12 w-12 shrink-0 place-items-center rounded-xl bg-slate-100 text-slate-600"><Icon name={fileIcon(it.file.name)} className="h-7 w-7" /></span>
                <div className="min-w-0">
                  <p className="truncate text-sm font-semibold" title={it.file.name}>{it.file.name}</p>
                  <p className="text-xs text-slate-500">{formatBytes(it.file.size)}</p>
                </div>
              </div>
              <StateLine it={it} />
              {it.state === "uploading" && <ProgressBar value={it.progress} label={`Uploading ${it.file.name}`} />}
              {it.message && <p className={cx("text-xs", it.state === "duplicate" ? "text-amber-700" : "text-red-700")}>{it.message}</p>}
            </li>
          ))}
        </ul>
      </section>

      <aside className="lg:sticky lg:top-24 lg:self-start">
        <div className="card space-y-4 p-5">
          <h2 className="text-xl font-bold">Extract data</h2>
          <p className="text-sm text-slate-600">
            {busy ? <span className="inline-flex items-center gap-1.5"><Spinner /> Uploading…</span>
              : ready > 0 ? `${ready} file${ready > 1 ? "s" : ""} ready (${formatBytes(readyBytes)}). Each is read, checked and added to your documents.`
              : done > 0 ? `${done} of ${items.length} uploaded.` : "Nothing to upload yet."}
            {failed > 0 && !busy && <span className="ml-1 text-red-700">{failed} failed.</span>}
          </p>
          {done > 0 && !busy && ready === 0 ? (
            <Link className="btn btn-primary btn-xl w-full !px-4" href="/documents">View documents <Icon name="arrow" className="h-5 w-5" /></Link>
          ) : (
            <button className="btn btn-primary btn-xl w-full !px-4" disabled={busy || ready === 0} onClick={start}>
              Extract data <Icon name="arrow" className="h-5 w-5" />
            </button>
          )}
          <div className="flex flex-wrap gap-2">
            {failed > 0 && !busy && <button className="btn" onClick={retryFailed}>Retry failed</button>}
            {!busy && <button className="btn" onClick={() => setItems([])}>Clear list</button>}
            {done > 0 && ready > 0 && !busy && <Link className="btn" href="/documents">View documents</Link>}
          </div>
        </div>
      </aside>
      {picker}
    </div>
  );
}
