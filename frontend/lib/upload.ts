import type { Info } from "./types";

export const FALLBACK_EXTENSIONS = [".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".xlsx", ".xls", ".csv"];

export const extensionOf = (name: string): string => {
  const i = name.lastIndexOf(".");
  return i <= 0 ? "" : name.slice(i).toLowerCase();
};

/** Client-side check before uploading. The server checks again (and also looks inside the file). */
export function checkFile(file: { name: string; size: number }, info: Pick<Info, "extensions" | "max_file_size_mb"> | null): string | null {
  const allowed = info?.extensions ?? FALLBACK_EXTENSIONS;
  const ext = extensionOf(file.name);
  if (!allowed.includes(ext)) return `${ext ? `"${ext}" files are` : "Files without an extension are"} not supported. Use ${allowed.join(", ")}.`;
  if (file.size === 0) return "The file is empty.";
  const maxMb = info?.max_file_size_mb ?? 200;
  if (file.size > maxMb * 1024 * 1024) return `The file is larger than ${maxMb} MB.`;
  return null;
}

/** Run `worker` over `items` with at most `limit` running at once. Never rejects; errors belong in the worker. */
export async function runPool<T>(items: T[], limit: number, worker: (item: T) => Promise<void>): Promise<void> {
  let next = 0;
  const lane = async () => {
    while (next < items.length) {
      const item = items[next++];
      try { await worker(item); } catch { /* the worker reports its own errors */ }
    }
  };
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, lane));
}
