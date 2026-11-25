"use client";
import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { ErrorState, LoadingBlock } from "./ui";

/** Shows the original file. Fetched with the token and shown from a blob URL, because <iframe src> and <img src>
 *  cannot send an Authorization header. Only PDF and JPG/PNG can be shown by a browser; the rest are downloads. */
const PREVIEWABLE = new Set(["pdf", "jpeg", "png"]);

export function FilePreview({ id, fileType, filename }: { id: string; fileType: string; filename: string }) {
  const [url, setUrl] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(false);
  const canShow = PREVIEWABLE.has(fileType);

  const load = useCallback(() => {
    const ctrl = new AbortController();
    let objectUrl: string | null = null;
    setLoading(true); setError(null);
    api.file(id, ctrl.signal)
      .then((blob) => { objectUrl = URL.createObjectURL(blob); setUrl(objectUrl); })
      .catch((e) => { if ((e as Error).name !== "AbortError") setError(e); })
      .finally(() => setLoading(false));
    return () => { ctrl.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [id]);

  useEffect(() => (canShow ? load() : undefined), [canShow, load]);

  if (!canShow) {
    return (
      <div className="flex h-full min-h-[10rem] flex-col items-center justify-center gap-3 p-6 text-center text-sm text-slate-600">
        <p>A preview is not available for <span className="font-mono uppercase">{fileType}</span> files.</p>
        <DownloadButton id={id} filename={filename} />
      </div>
    );
  }
  if (loading) return <LoadingBlock label="Loading preview…" />;
  if (error) return <div className="p-3"><ErrorState error={error} onRetry={load} title="Could not load the file" /></div>;
  if (!url) return null;
  return (
    <div className="flex h-full flex-col">
      <div className="flex shrink-0 justify-end border-b border-slate-200 bg-slate-50 px-3 py-1 text-xs">
        {/* some phone browsers cannot show a PDF inside a page; a new tab always works */}
        <a href={url} target="_blank" rel="noopener noreferrer" className="text-brand-700 hover:underline">Open in a new tab ↗</a>
      </div>
      {fileType === "pdf"
        ? <iframe src={url} title={`Preview of ${filename}`} className="min-h-[20rem] w-full flex-1 border-0 bg-white" />
        // eslint-disable-next-line @next/next/no-img-element
        : <div className="min-h-[14rem] flex-1 overflow-auto bg-slate-100 p-2"><img src={url} alt={`Scan of ${filename}`} className="mx-auto max-w-full" /></div>}
    </div>
  );
}

export function DownloadButton({ id, filename }: { id: string; filename: string }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function download() {
    setBusy(true); setError(null);
    try {
      const blob = await api.file(id);
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = filename;
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 10_000);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Download failed");
    } finally { setBusy(false); }
  }
  return (
    <span className="inline-flex flex-col items-start">
      <button className="btn" onClick={download} disabled={busy}>{busy ? "Downloading…" : "Download original"}</button>
      {error && <span className="mt-1 text-xs text-red-700">{error}</span>}
    </span>
  );
}
