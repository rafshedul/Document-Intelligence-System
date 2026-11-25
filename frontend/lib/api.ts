// Typed client for the backend. The only place that knows URLs, the token and the error format.
import type {
  CorrectionEntry, CorrectionRequest, DocumentList, Info, ResultResponse, SearchMode, SearchResponse,
  StatusResponse, UploadFileResult,
} from "./types";

export const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000").replace(/\/+$/, "");
const TOKEN_KEY = "docintel_token";
export const UNAUTHORIZED_EVENT = "docintel:unauthorized";

export const getToken = (): string | null =>
  typeof window === "undefined" ? null : window.sessionStorage.getItem(TOKEN_KEY);
export const setToken = (t: string): void => window.sessionStorage.setItem(TOKEN_KEY, t);
export const clearToken = (): void => window.sessionStorage.removeItem(TOKEN_KEY);

/** Every backend error has the shape {"error": {code, message, details?, request_id}}. */
export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
    public code: string,
    public details?: unknown,
    public requestId?: string | null,
  ) {
    super(message);
    this.name = "ApiError";
  }

  /** Per-field problems from a 422, keyed by field name ("fields.total" -> "total", "line_items.2.quantity" -> "2.quantity"). */
  get problems(): Record<string, string> {
    const list = (this.details as { problems?: { field: string; message: string }[] } | undefined)?.problems ?? [];
    return Object.fromEntries(list.map((p) => [p.field.replace(/^(fields|line_items)\./, ""), p.message]));
  }

  /** For a rejected duplicate upload: the id of the document that already exists. */
  get existingDocumentId(): string | null {
    const files = (this.details as { files?: UploadFileResult[] } | undefined)?.files;
    return files?.[0]?.error?.existing_document_id ?? null;
  }
}

export function toApiError(status: number, body: unknown, fallback: string): ApiError {
  const e = (body as { error?: { code?: string; message?: string; details?: unknown; request_id?: string } } | null)?.error;
  if (e && typeof e.message === "string") {
    return new ApiError(e.message, status, e.code ?? "error", e.details, e.request_id);
  }
  return new ApiError(fallback, status, "error");
}

const networkError = () => new ApiError(`Cannot reach the server at ${API_URL}. Is the backend running?`, 0, "network_error");

function authHeaders(extra: Record<string, string> = {}): Record<string, string> {
  const t = getToken();
  return t ? { ...extra, Authorization: `Bearer ${t}` } : extra;
}

function onUnauthorized(status: number): void {
  if (status === 401 && typeof window !== "undefined") {
    clearToken();
    window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
  }
}

async function request<T>(path: string, init: { method?: string; json?: unknown; signal?: AbortSignal } = {}): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_URL}${path}`, {
      method: init.method ?? "GET",
      headers: authHeaders(init.json !== undefined ? { "Content-Type": "application/json" } : {}),
      body: init.json !== undefined ? JSON.stringify(init.json) : undefined,
      signal: init.signal,
    });
  } catch (err) {
    if ((err as Error).name === "AbortError") throw err;
    throw networkError();
  }
  if (res.status === 204) return undefined as T;
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    onUnauthorized(res.status);
    throw toApiError(res.status, body, `Request failed (${res.status}).`);
  }
  return body as T;
}

export interface ListParams { status?: string; file_type?: string; page?: number; page_size?: number }
export interface SearchParams {
  q: string; mode: SearchMode; supplier?: string; date_from?: string; date_to?: string; status?: string;
  page?: number; page_size?: number;
}

export function query(params: object): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") q.set(k, String(v));
  const s = q.toString();
  return s ? `?${s}` : "";
}

export const api = {
  info: (signal?: AbortSignal) => request<Info>("/api/info", { signal }),
  listDocuments: (p: ListParams = {}, signal?: AbortSignal) => request<DocumentList>(`/api/documents${query(p)}`, { signal }),
  status: (id: string, signal?: AbortSignal) => request<StatusResponse>(`/api/documents/${id}/status`, { signal }),
  result: (id: string, signal?: AbortSignal) => request<ResultResponse>(`/api/documents/${id}/result`, { signal }),
  corrections: (id: string, signal?: AbortSignal) => request<CorrectionEntry[]>(`/api/documents/${id}/corrections`, { signal }),
  correct: (id: string, body: CorrectionRequest) => request<ResultResponse>(`/api/documents/${id}/result`, { method: "PATCH", json: body }),
  reprocess: (id: string) => request<{ id: string; status: string; attempt: number }>(`/api/documents/${id}/reprocess`, { method: "POST" }),
  remove: (id: string) => request<void>(`/api/documents/${id}`, { method: "DELETE" }),
  search: (p: SearchParams, signal?: AbortSignal) => request<SearchResponse>(`/api/search${query(p)}`, { signal }),

  /** The original file as a Blob (a plain <iframe src> cannot send the Authorization header). */
  async file(id: string, signal?: AbortSignal): Promise<Blob> {
    let res: Response;
    try {
      res = await fetch(`${API_URL}/api/documents/${id}/file`, { headers: authHeaders(), signal });
    } catch (err) {
      if ((err as Error).name === "AbortError") throw err;
      throw networkError();
    }
    if (!res.ok) {
      onUnauthorized(res.status);
      throw toApiError(res.status, await res.json().catch(() => null), "The file could not be loaded.");
    }
    return res.blob();
  },
};

/** Upload one file, reporting progress 0-100. (fetch cannot report upload progress, XMLHttpRequest can.) */
export function uploadFile(file: File, onProgress: (pct: number) => void, signal?: AbortSignal): Promise<UploadFileResult> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${API_URL}/api/documents/upload`);
    const t = getToken();
    if (t) xhr.setRequestHeader("Authorization", `Bearer ${t}`);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress(Math.round((e.loaded / e.total) * 100));
    };
    xhr.onload = () => {
      let body: unknown = null;
      try { body = JSON.parse(xhr.responseText); } catch { /* not JSON */ }
      if (xhr.status === 202) {
        const first = (body as { files?: UploadFileResult[] } | null)?.files?.[0];
        if (first) return resolve(first);
      }
      onUnauthorized(xhr.status);
      reject(toApiError(xhr.status, body, `Upload failed (${xhr.status}).`));
    };
    xhr.onerror = () => reject(networkError());
    xhr.onabort = () => reject(new DOMException("Aborted", "AbortError"));
    signal?.addEventListener("abort", () => xhr.abort());
    const form = new FormData();
    form.append("files", file);
    xhr.send(form);
  });
}
