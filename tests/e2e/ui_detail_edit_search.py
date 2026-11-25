"""Browser test: live progress, document detail, editing, reprocess, previews and search.
Run ui_upload_and_list.py first (it uploads the sample files this one opens).
"""
import io, os, re, sys, time
import pymupdf
from PIL import Image
from playwright.sync_api import sync_playwright, expect

BASE = os.environ.get("E2E_BASE", "http://localhost:3000"); SD = os.environ.get("E2E_SAMPLES", os.path.join(os.path.dirname(__file__), "..", "..", "sample_data")); SHOTS = os.environ.get("E2E_SHOTS", "/tmp/docintel-e2e/shots")
os.makedirs(SHOTS, exist_ok=True)
os.makedirs("/tmp/docintel-e2e", exist_ok=True)  # scratch files (the upload test creates a few)
problems = []
def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond: problems.append(msg)

# a slow 30-page scan, to watch live progress
jpg = io.BytesIO(); Image.open(f"{SD}/scanned_invoice.jpg").save(jpg, "JPEG", quality=60)
with pymupdf.open() as d:
    for i in range(30): d.new_page(width=595, height=842).insert_image(pymupdf.Rect(0, 0, 595, 842), stream=jpg.getvalue())
    d.save("/tmp/docintel-e2e/slow2.pdf")

with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 1280, "height": 900}); page = ctx.new_page()
    errs = []; page.on("pageerror", lambda e: errs.append(str(e)))

    # ---------- live polling in the list ----------
    page.goto(BASE + "/upload")
    page.set_input_files("[data-testid=file-input]", ["/tmp/docintel-e2e/slow2.pdf"])
    page.get_by_role("button", name="Extract data").click()
    page.wait_for_selector("text=✓ Uploaded", timeout=15000)
    page.goto(BASE + "/documents"); page.wait_for_selector("table tbody tr")
    page.evaluate("window.__marker = 'no-reload'")
    vals = []
    t0 = time.time()
    while time.time() - t0 < 60:  # the server may still be busy with the large CSV from the previous script
        bar = page.locator("tr:has-text('slow2.pdf') [role=progressbar]")
        if bar.count():
            v = int(bar.first.get_attribute("aria-valuenow"))
            if not vals or vals[-1] != v: vals.append(v)
            if len(vals) >= 4: break
        elif vals: break
        page.wait_for_timeout(500)
    check(len(vals) >= 3 and vals == sorted(vals), f"list progress updates live, in steps, never goes backwards: {vals}")
    check(page.evaluate("window.__marker") == "no-reload", "the list updated by polling, without reloading the page")
    page.screenshot(path=f"{SHOTS}/04_live_progress.png")

    # ---------- detail page ----------
    page.goto(BASE + "/documents"); page.wait_for_selector("table tbody tr")
    page.locator("table a", has_text="digital_invoice.pdf").click()
    page.wait_for_selector("#field-total")
    check("INV-2026-0042" in page.locator("#field-invoice_number").inner_text(), "detail: invoice number shown")
    check("757.05" in page.locator("#field-total").inner_text(), "detail: total shown")
    check("80%" in page.locator("#field-total").inner_text(), "detail: confidence next to the field")
    check(page.get_by_text("All checks passed").is_visible(), "detail: validation panel says all checks passed")
    check(page.locator("#li-1").count() == 1 and page.locator("[id^=li-]").count() == 3, "detail: 3 line items in a table")
    page.wait_for_selector("iframe[title^='Preview of']", timeout=10000)
    check(page.locator("iframe[title^='Preview of']").get_attribute("src").startswith("blob:"), "detail: PDF preview shown from an authorised blob")
    page.screenshot(path=f"{SHOTS}/05_detail_pdf.png", full_page=False)

    # ---------- edit mode ----------
    page.locator("#field-total [role=button]").click()      # click the value -> edit mode
    check(page.get_by_text("Editing").first.is_visible(), "clicking a value enters edit mode")
    inp = page.locator("#field-total input"); expect(inp).to_be_focused()
    inp.fill("abc")
    page.get_by_role("button", name="Save").click()
    check(page.locator("#field-total").get_by_role("alert").is_visible(), "a bad amount is refused before saving, with a message next to the field")
    inp.fill("800.00")
    page.get_by_role("button", name="Save").click()
    page.wait_for_selector("text=Saved. Validation ran again")
    check("800.00" in page.locator("#field-total").inner_text(), "saved value is shown")
    check("edited by hand" in page.locator("#field-total").inner_text(), "the field is marked as edited by hand")
    page.wait_for_selector("text=TOTAL_MISMATCH")
    check(page.get_by_text("TOTAL_MISMATCH").first.is_visible(), "validation re-ran: TOTAL_MISMATCH appears")
    check("needs review" in page.locator("h1").inner_text().lower(), "status became needs review")
    page.locator("[aria-labelledby=validation-title] button", has_text="total").first.click()
    page.wait_for_timeout(300)
    check(page.locator("#field-total").evaluate("e => e.className").count("border-red-500") == 1, "the total row is marked red (linked to the error)")
    hist = page.locator("details", has_text="Correction history")
    check("800.00" in hist.inner_text() and "757.05" in hist.inner_text() and "web" in hist.inner_text(), "correction history lists old value, new value and who")
    page.screenshot(path=f"{SHOTS}/06_detail_edited.png", full_page=True)

    # edit a line item, and fix the total back
    page.get_by_role("button", name="Edit values").click()
    page.locator("#field-total input").fill("757.05")
    page.get_by_role("button", name="Save").click()
    page.wait_for_selector("text=Saved. Validation ran again")
    page.wait_for_function("!document.body.innerText.includes('TOTAL_MISMATCH')")
    check("completed" in page.locator("h1").inner_text().lower(), "fixing the value clears the error and the document is completed again")

    # ---------- failed document: reprocess ----------
    page.goto(BASE + "/documents"); page.wait_for_selector("table tbody tr")
    page.locator("table a", has_text="corrupted.pdf").click()
    page.wait_for_selector("text=Processing failed")
    check(page.get_by_role("button", name="Reprocess").is_visible(), "failed document has a Reprocess button")
    check(page.get_by_text("No data was extracted").is_visible(), "failed document shows an empty state instead of blank fields")
    page.get_by_role("button", name="Reprocess").click()
    page.wait_for_selector("text=Queued again")
    page.wait_for_selector("text=Processing failed", timeout=15000)
    page.screenshot(path=f"{SHOTS}/07_failed_detail.png")

    # image preview
    page.goto(BASE + "/documents"); page.wait_for_selector("table tbody tr")
    page.locator("table a", has_text="scanned_invoice.jpg").click()
    page.wait_for_selector("img[alt^='Scan of']", timeout=10000)
    check(page.locator("img[alt^='Scan of']").evaluate("i => i.complete && i.naturalWidth > 100"), "image preview loads")
    low = page.locator("[id^=field-]").filter(has_text="⚠").count()
    print("fields flagged low-confidence on the scan:", low)
    page.screenshot(path=f"{SHOTS}/08_detail_image.png")
    # spreadsheet: no preview, download offered
    page.goto(BASE + "/documents"); page.wait_for_selector("table tbody tr")
    page.locator("table a", has_text="invoices.csv").click()
    page.wait_for_selector("text=A preview is not available")
    check(page.get_by_role("button", name="Download original").count() >= 1, "spreadsheet: no preview, download button instead")

    # ---------- search ----------
    page.goto(BASE + "/search"); page.wait_for_selector("#q")
    check(page.get_by_text("Type something to search").is_visible(), "search: empty state before a query")
    page.fill("#q", "hosting"); page.get_by_role("button", name="Search", exact=True).click()
    page.wait_for_selector("mark")
    check(page.locator("mark").first.inner_text().lower() == "hosting", "search: matched word is highlighted")
    check(page.locator("li.card").count() >= 3, "search: several documents found")
    check("p.1" in page.locator("li.card").first.inner_text(), "search: page number shown with the snippet")
    page.screenshot(path=f"{SHOTS}/09_search_hybrid.png", full_page=True)
    page.get_by_role("radio", name="Keyword").click(); page.wait_for_url(re.compile("mode=keyword"))
    page.wait_for_selector("mark")
    check("keyword" in page.locator("li.card").first.inner_text(), "search: keyword mode")
    page.get_by_role("radio", name="Semantic").click(); page.wait_for_url(re.compile("mode=semantic"))
    page.wait_for_selector("li.card"); check("semantic" in page.locator("li.card").first.inner_text(), "search: semantic mode")
    page.get_by_role("radio", name="Hybrid").click(); page.wait_for_url(re.compile("mode=hybrid"))
    page.fill("#supplier", "globex"); page.get_by_role("button", name="Search", exact=True).click()
    page.wait_for_selector("text=No results for")
    check(page.get_by_text("Try removing a filter").is_visible(), "search: empty state with a hint when filters remove everything")
    page.get_by_role("button", name="Clear filters").first.click()
    page.wait_for_selector("li.card")
    page.fill("#from", "2026-11-01"); page.fill("#to", "2026-11-30"); page.get_by_role("button", name="Search", exact=True).click()
    page.wait_for_url(re.compile("date_from=2026-11-01")); page.wait_for_selector("li.card")
    check(page.url.count("date_from=2026-11-01") == 1, "search: filters live in the URL (refresh keeps them)")
    page.reload(); page.wait_for_selector("li.card")
    check(page.input_value("#q") == "hosting" and page.input_value("#from") == "2026-11-01", "search: state survives a refresh")
    page.locator("li.card a").first.click(); page.wait_for_selector("#field-total")
    check("/documents/" in page.url, "search: result opens the document")
    errs_f = [e for e in errs if "hydrat" in e.lower()]
    check(not errs_f, f"no hydration/runtime errors: {errs[:3]}")
    b.close()
sys.exit(1 if problems else 0)
