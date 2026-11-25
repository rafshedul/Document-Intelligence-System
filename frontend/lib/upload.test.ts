import { describe, expect, it } from "vitest";
import { checkFile, extensionOf, FALLBACK_EXTENSIONS, runPool } from "./upload";

const info = { extensions: [".pdf", ".png", ".csv"], max_file_size_mb: 2 };
const MB = 1024 * 1024;

describe("extensionOf", () => {
  it.each([["a.PDF", ".pdf"], ["x.tar.gz", ".gz"], ["noext", ""], [".hidden", ""], ["a.", "."], ["dir/file.Csv", ".csv"]])("%s -> %s", (n, e) => expect(extensionOf(n)).toBe(e));
});

describe("checkFile", () => {
  it("accepts an allowed file in any letter case", () => {
    expect(checkFile({ name: "inv.PDF", size: 1000 }, info)).toBeNull();
    expect(checkFile({ name: "inv.csv", size: 2 * MB }, info)).toBeNull(); // exactly the limit is fine
  });
  it("rejects types that are not allowed, and says which are", () => {
    const m = checkFile({ name: "notes.docx", size: 10 }, info);
    expect(m).toContain(".docx");
    expect(m).toContain(".pdf, .png, .csv");
  });
  it("rejects files with no extension", () => expect(checkFile({ name: "README", size: 10 }, info)).toMatch(/without an extension/));
  it("rejects empty and oversized files", () => {
    expect(checkFile({ name: "a.pdf", size: 0 }, info)).toMatch(/empty/);
    expect(checkFile({ name: "a.pdf", size: 2 * MB + 1 }, info)).toMatch(/larger than 2 MB/);
  });
  it("uses safe defaults before the server settings are known", () => {
    expect(checkFile({ name: "a.xlsx", size: 5 }, null)).toBeNull();
    expect(checkFile({ name: "a.exe", size: 5 }, null)).not.toBeNull();
    expect(checkFile({ name: "a.pdf", size: 201 * MB }, null)).toMatch(/200 MB/);
    expect(FALLBACK_EXTENSIONS).toContain(".tiff");
  });
});

describe("runPool", () => {
  it("never runs more than `limit` at once, and runs everything", async () => {
    let active = 0, peak = 0;
    const done: number[] = [];
    await runPool([1, 2, 3, 4, 5, 6, 7], 3, async (n) => {
      active++; peak = Math.max(peak, active);
      await new Promise((r) => setTimeout(r, 5));
      done.push(n); active--;
    });
    expect(peak).toBe(3);
    expect(done.sort()).toEqual([1, 2, 3, 4, 5, 6, 7]);
  });
  it("keeps going when one item fails", async () => {
    const done: number[] = [];
    await runPool([1, 2, 3], 2, async (n) => { if (n === 2) throw new Error("boom"); done.push(n); });
    expect(done.sort()).toEqual([1, 3]);
  });
  it("copes with no items", async () => { await expect(runPool([], 3, async () => {})).resolves.toBeUndefined(); });
});
