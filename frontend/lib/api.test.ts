import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, api, query, toApiError, uploadFile, UNAUTHORIZED_EVENT } from "./api";

const store = new Map<string, string>();
const events: string[] = [];

beforeEach(() => {
  store.clear(); events.length = 0;
  (globalThis as any).window = {
    sessionStorage: { getItem: (k: string) => store.get(k) ?? null, setItem: (k: string, v: string) => store.set(k, v), removeItem: (k: string) => store.delete(k) },
    dispatchEvent: (e: Event) => { events.push(e.type); return true; },
  };
});
afterEach(() => { vi.unstubAllGlobals(); delete (globalThis as any).window; });

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const errBody = (code: string, message: string, details?: unknown) => ({ error: { code, message, details, request_id: "abc123" } });

describe("error format", () => {
  it("reads the backend's uniform error", () => {
    const e = toApiError(409, errBody("duplicate_file", "Already there."), "fallback");
    expect(e).toBeInstanceOf(ApiError);
    expect([e.status, e.code, e.message, e.requestId]).toEqual([409, "duplicate_file", "Already there.", "abc123"]);
  });
  it("falls back for anything else, without throwing", () => {
    for (const body of [null, "html page", { detail: "x" }, { error: "str" }, undefined]) {
      expect(toApiError(500, body, "Request failed.").message).toBe("Request failed.");
    }
  });
  it("turns 422 problems into a map by field", () => {
    const e = toApiError(422, errBody("validation_error", "bad", { problems: [{ field: "fields.total", message: "not a number" }, { field: "line_items.2.quantity", message: "too many decimals" }] }), "");
    expect(e.problems).toEqual({ total: "not a number", "2.quantity": "too many decimals" });
    expect(toApiError(500, null, "x").problems).toEqual({});
  });
  it("finds the existing document of a duplicate upload", () => {
    const e = toApiError(409, errBody("duplicate_file", "dup", { files: [{ error: { existing_document_id: "doc-9" } }] }), "");
    expect(e.existingDocumentId).toBe("doc-9");
    expect(toApiError(409, errBody("x", "y"), "").existingDocumentId).toBeNull();
  });
});

describe("query", () => {
  it("skips empty values and encodes the rest", () => {
    expect(query({ a: 1, b: "", c: undefined, d: null, e: "x y&z", f: 0 })).toBe("?a=1&e=x+y%26z&f=0");
    expect(query({})).toBe("");
  });
});

describe("requests", () => {
  it("sends the token and parses the answer", async () => {
    store.set("docintel_token", "secret");
    const fetchMock = vi.fn().mockResolvedValue(json(200, { items: [], total: 0, page: 1, page_size: 20, pages: 0 }));
    vi.stubGlobal("fetch", fetchMock);
    const r = await api.listDocuments({ status: "failed", page: 2 });
    expect(r.total).toBe(0);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toMatch(/\/api\/documents\?status=failed&page=2$/);
    expect(init.headers.Authorization).toBe("Bearer secret");
  });
  it("sends JSON bodies with a content type", async () => {
    const fetchMock = vi.fn().mockResolvedValue(json(200, {}));
    vi.stubGlobal("fetch", fetchMock);
    await api.correct("id1", { fields: { total: "5" } });
    const [, init] = fetchMock.mock.calls[0];
    expect(init.method).toBe("PATCH");
    expect(init.headers["Content-Type"]).toBe("application/json");
    expect(JSON.parse(init.body)).toEqual({ fields: { total: "5" } });
  });
  it("throws an ApiError carrying the server's message", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json(404, errBody("not_found", "Document not found."))));
    await expect(api.status("x")).rejects.toMatchObject({ status: 404, code: "not_found", message: "Document not found." });
  });
  it("handles 204 with no body", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 204 })));
    await expect(api.remove("x")).resolves.toBeUndefined();
  });
  it("reports an unreachable server in plain words", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    await expect(api.info()).rejects.toMatchObject({ status: 0, code: "network_error", message: expect.stringContaining("Cannot reach the server") });
  });
  it("lets an aborted request stay an abort", async () => {
    const abort = Object.assign(new Error("aborted"), { name: "AbortError" });
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(abort));
    await expect(api.info()).rejects.toMatchObject({ name: "AbortError" });
  });
  it("on 401 forgets the token and tells the app to ask for a new one", async () => {
    store.set("docintel_token", "old");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json(401, errBody("unauthorized", "Invalid or missing token."))));
    await expect(api.info()).rejects.toMatchObject({ status: 401 });
    expect(store.has("docintel_token")).toBe(false);
    expect(events).toContain(UNAUTHORIZED_EVENT);
  });
  it("downloads the original file as a blob with the token", async () => {
    store.set("docintel_token", "t");
    const fetchMock = vi.fn().mockResolvedValue(new Response("%PDF-1.4", { status: 200, headers: { "content-type": "application/pdf" } }));
    vi.stubGlobal("fetch", fetchMock);
    const blob = await api.file("d1");
    expect(blob.type).toBe("application/pdf");
    expect(fetchMock.mock.calls[0][1].headers.Authorization).toBe("Bearer t");
  });
  it("explains a failed file download", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json(404, errBody("not_found", "The stored file is missing."))));
    await expect(api.file("d1")).rejects.toMatchObject({ message: "The stored file is missing." });
  });
});

// ---- upload progress needs XMLHttpRequest; a small fake stands in for it ----

class FakeXHR {
  static last: FakeXHR;
  upload: { onprogress?: (e: unknown) => void } = {};
  headers: Record<string, string> = {};
  status = 0; responseText = "";
  onload?: () => void; onerror?: () => void; onabort?: () => void;
  opened: [string, string] | null = null; sent: FormData | null = null;
  constructor() { FakeXHR.last = this; }
  open(m: string, u: string) { this.opened = [m, u]; }
  setRequestHeader(k: string, v: string) { this.headers[k] = v; }
  send(body: FormData) { this.sent = body; }
  abort() { this.onabort?.(); }
  reply(status: number, body: unknown) { this.status = status; this.responseText = JSON.stringify(body); this.onload?.(); }
}

describe("uploadFile", () => {
  beforeEach(() => vi.stubGlobal("XMLHttpRequest", FakeXHR));
  const file = new File(["a,b\n1,2\n"], "inv.csv", { type: "text/csv" });

  it("reports progress and resolves with the stored document", async () => {
    store.set("docintel_token", "tok");
    const seen: number[] = [];
    const p = uploadFile(file, (n) => seen.push(n));
    const x = FakeXHR.last;
    x.upload.onprogress?.({ lengthComputable: true, loaded: 25, total: 100 });
    x.upload.onprogress?.({ lengthComputable: true, loaded: 100, total: 100 });
    x.upload.onprogress?.({ lengthComputable: false, loaded: 0, total: 0 });
    x.reply(202, { accepted: 1, rejected: 0, files: [{ filename: "inv.csv", status: "queued", id: "doc1", error: null }] });
    await expect(p).resolves.toMatchObject({ id: "doc1", status: "queued" });
    expect(seen).toEqual([25, 100]);
    expect(x.opened?.[0]).toBe("POST");
    expect(x.opened?.[1]).toMatch(/\/api\/documents\/upload$/);
    expect(x.headers.Authorization).toBe("Bearer tok");
    expect((x.sent as FormData).get("files")).toBeInstanceOf(File);
  });
  it("rejects with the server's reason, e.g. a duplicate", async () => {
    const p = uploadFile(file, () => {});
    FakeXHR.last.reply(409, errBody("duplicate_file", "Already uploaded.", { files: [{ error: { existing_document_id: "d7" } }] }));
    await expect(p).rejects.toMatchObject({ status: 409, code: "duplicate_file", existingDocumentId: "d7" });
  });
  it("rejects when the network drops, and when aborted", async () => {
    const a = uploadFile(file, () => {});
    FakeXHR.last.onerror?.();
    await expect(a).rejects.toMatchObject({ code: "network_error" });
    const ctrl = new AbortController();
    const b = uploadFile(file, () => {}, ctrl.signal);
    ctrl.abort();
    await expect(b).rejects.toMatchObject({ name: "AbortError" });
  });
  it("handles a non-JSON error page", async () => {
    const p = uploadFile(file, () => {});
    FakeXHR.last.status = 502; FakeXHR.last.responseText = "<html>Bad gateway</html>"; FakeXHR.last.onload?.();
    await expect(p).rejects.toMatchObject({ status: 502, message: "Upload failed (502)." });
  });
  it("treats an unexpected 202 body as a failure instead of crashing", async () => {
    const p = uploadFile(file, () => {});
    FakeXHR.last.reply(202, { weird: true });
    await expect(p).rejects.toBeInstanceOf(ApiError);
  });
});
