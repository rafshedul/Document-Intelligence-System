import { describe, expect, it } from "vitest";
import { confidencePct, formatBytes, formatDateTime, isLowConfidence, LOW_CONFIDENCE, splitHighlights, statusLabel } from "./format";

describe("formatBytes", () => {
  it.each([[0, "0 B"], [1023, "1023 B"], [1536, "1.5 KB"], [20480, "20 KB"], [5 * 1024 * 1024, "5.0 MB"]])("%i -> %s", (n, s) => expect(formatBytes(n)).toBe(s));
});

describe("confidence", () => {
  it("is low only below the 0.7 threshold the backend uses", () => {
    expect(LOW_CONFIDENCE).toBe(0.7);
    expect(isLowConfidence(0.69)).toBe(true);
    expect(isLowConfidence(0.7)).toBe(false);
    expect(isLowConfidence(0.95)).toBe(false);
  });
  it("has no opinion about a missing value", () => {
    expect(isLowConfidence(null)).toBe(false);
    expect(isLowConfidence(undefined)).toBe(false);
  });
  it("rounds to a percentage and stays within 0-100", () => {
    expect(confidencePct(0.944)).toBe(94);
    expect(confidencePct(1.4)).toBe(100);
    expect(confidencePct(-1)).toBe(0);
  });
});

describe("misc", () => {
  it("formats dates and survives garbage", () => {
    expect(formatDateTime("2026-11-24T10:00:00+06:00")).toMatch(/2026/);
    expect(formatDateTime(null)).toBe("—");
    expect(formatDateTime("not a date")).toBe("—");
  });
  it("makes status names readable", () => expect(statusLabel("needs_review")).toBe("needs review"));
});

describe("splitHighlights", () => {
  const join = (s: { text: string }[]) => s.map((x) => x.text).join("");

  it("cuts the text at the ranges", () => {
    const segs = splitHighlights("Acme Hosting Ltd", [[5, 12]]);
    expect(segs).toEqual([{ text: "Acme ", mark: false }, { text: "Hosting", mark: true }, { text: " Ltd", mark: false }]);
  });
  it("handles several ranges, in any order, and the very start and end", () => {
    const segs = splitHighlights("abcdef", [[4, 6], [0, 2]]);
    expect(segs.map((s) => [s.text, s.mark])).toEqual([["ab", true], ["cd", false], ["ef", true]]);
  });
  it("never loses or repeats text, whatever the ranges are", () => {
    const text = "some text here";
    for (const ranges of [[], [[0, 0]], [[-3, 2]], [[5, 100]], [[2, 4], [3, 6]], [[1.5, 3]], [[20, 30]], [[0, 14]], [[3]]]) {
      expect(join(splitHighlights(text, ranges as number[][]))).toBe(text);
    }
  });
  it("ignores overlapping, reversed and out-of-range ranges instead of crashing", () => {
    expect(splitHighlights("abcdef", [[2, 4], [3, 5]]).filter((s) => s.mark).map((s) => s.text)).toEqual(["cd"]);
    expect(splitHighlights("abc", [[2, 1]]).some((s) => s.mark)).toBe(false);
    expect(splitHighlights("abc", [[1, 99]]).filter((s) => s.mark).map((s) => s.text)).toEqual(["bc"]);
  });
  it("returns the text unchanged for no ranges and for empty text", () => {
    expect(splitHighlights("plain", [])).toEqual([{ text: "plain", mark: false }]);
    expect(splitHighlights("", [[0, 1]])).toEqual([{ text: "", mark: false }]);
  });
  it("treats markup in the text as plain text", () => {
    const segs = splitHighlights("<script>x</script> hosting", [[19, 26]]);
    expect(segs.find((s) => s.mark)?.text).toBe("hosting");
    expect(join(segs)).toBe("<script>x</script> hosting"); // React escapes it when rendering
  });
});
