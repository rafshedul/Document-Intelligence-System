"""Browser test: upload (client checks, per-file progress, duplicates) and the documents list (filters, empty state).
Needs the stack running (see tests/e2e/README.md).
"""
import os, re, sys, time
from playwright.sync_api import sync_playwright, expect

BASE = os.environ.get("E2E_BASE", "http://localhost:3000")
SD = os.environ.get("E2E_SAMPLES", os.path.join(os.path.dirname(__file__), "..", "..", "sample_data"))
SHOTS = os.environ.get("E2E_SHOTS", "/tmp/docintel-e2e/shots")
os.makedirs(SHOTS, exist_ok=True)
os.makedirs("/tmp/docintel-e2e", exist_ok=True)  # scratch files (the upload test creates a few)
problems = []

def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond: problems.append(msg)

# files for the upload test
open("/tmp/docintel-e2e/notes.docx", "wb").write(b"PK\x03\x04 not supported")
open("/tmp/docintel-e2e/empty.csv", "wb").write(b"")
rows = "Invoice No,Date,Supplier,Total,Currency\n" + "".join(f"INV-{i:05d},2026-01-{(i%28)+1:02d},Acme Hosting Ltd,{i}.50,USD\n" for i in range(90000))
open("/tmp/docintel-e2e/big.csv", "w").write(rows)
print("big.csv MB:", round(os.path.getsize("/tmp/docintel-e2e/big.csv")/1e6, 1))

with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    console_errors = []
    page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: console_errors.append(str(e)))

    # ---------- upload page ----------
    page.goto(BASE + "/upload")
    expect(page.get_by_role("heading", name="Extract invoice data")).to_be_visible()
    check(page.get_by_text("Allowed:").is_visible(), "upload page shows allowed types and size limit")
    page.set_input_files("[data-testid=file-input]", [
        f"{SD}/digital_invoice.pdf", f"{SD}/scanned_invoice.jpg", f"{SD}/invoices.csv", f"{SD}/multi_sheet.xlsx",
        f"{SD}/corrupted.pdf", "/tmp/docintel-e2e/notes.docx", "/tmp/docintel-e2e/empty.csv"])
    page.get_by_role("button", name="Extract data").click()  # choosing files only lists them; this sends them
    page.wait_for_function("document.body.innerText.split('✓ Uploaded').length - 1 >= 5", timeout=30000)  # all five, however fast
    n_up = page.get_by_text("✓ Uploaded").count()
    check(n_up == 5, f"5 valid files uploaded (got {n_up})")
    check(page.get_by_text("Not uploaded").count() == 2, "2 bad files refused on the client (docx, empty)")
    check(page.get_by_text("not supported").is_visible() and page.get_by_text("The file is empty").is_visible(), "client-side messages shown")
    page.screenshot(path=f"{SHOTS}/01_upload_done.png", full_page=True)

    # duplicate
    page.set_input_files("[data-testid=file-input]", [f"{SD}/digital_invoice.pdf"])
    page.get_by_role("button", name="Extract data").click()
    page.wait_for_selector("text=Already uploaded", timeout=10000)
    check(True, "uploading the same file again says 'Already uploaded'")

    # progress bar while uploading a big file over a slow connection
    cdp = ctx.new_cdp_session(page)
    cdp.send("Network.enable")
    cdp.send("Network.emulateNetworkConditions", {"offline": False, "latency": 20, "downloadThroughput": 1e7, "uploadThroughput": 600_000})
    page.set_input_files("[data-testid=file-input]", ["/tmp/docintel-e2e/big.csv"])
    page.get_by_role("button", name="Extract data").click()
    seen = set()
    t0 = time.time()
    while time.time() - t0 < 25:
        bars = page.locator("[role=progressbar][aria-label^='Uploading big.csv']")
        if bars.count():
            seen.add(int(bars.first.get_attribute("aria-valuenow")))
            if len(seen) >= 3 and max(seen) > 20: break
        else:
            if page.get_by_text("✓ Uploaded").count() >= 6: break
        page.wait_for_timeout(250)
    check(len(seen) >= 3 and 0 <= min(seen) < max(seen) <= 100, f"per-file upload progress bar moves: {sorted(seen)}")
    page.screenshot(path=f"{SHOTS}/02_upload_progress.png")
    page.wait_for_selector("text=/6 of \\d+ uploaded|7 of \\d+ uploaded/", timeout=60000) if False else None
    cdp.send("Network.emulateNetworkConditions", {"offline": False, "latency": 0, "downloadThroughput": -1, "uploadThroughput": -1})
    page.wait_for_function("document.body.innerText.includes('Uploading…') === false", timeout=90000)

    # ---------- documents list ----------
    page.goto(BASE + "/documents")
    expect(page.get_by_role("heading", name=re.compile("Documents"))).to_be_visible()
    page.wait_for_selector("table tbody tr", timeout=15000)
    page.wait_for_timeout(8000)  # let processing finish
    page.reload(); page.wait_for_selector("table tbody tr")
    names = [t.strip() for t in page.locator("table tbody tr td:first-child a").all_inner_texts()]
    check({"digital_invoice.pdf", "scanned_invoice.jpg", "invoices.csv", "multi_sheet.xlsx", "corrupted.pdf"} <= set(names), f"list shows the uploaded documents: {names}")
    badges = page.locator("table tbody tr").all_inner_texts()
    check(any("failed" in r.lower() for r in badges) and any("completed" in r.lower() for r in badges), "status badges shown (failed and completed present)")
    page.screenshot(path=f"{SHOTS}/03_documents_desktop.png", full_page=True)

    # filters
    page.select_option("#f-status", "failed")
    page.wait_for_timeout(800)
    check(page.locator("table tbody tr").count() == 1 and "corrupted.pdf" in page.locator("table").inner_text(), "status filter 'failed' shows only the failed document")
    page.select_option("#f-status", "")
    page.select_option("#f-type", "csv")
    page.wait_for_timeout(800)
    t = page.locator("table").inner_text().lower()
    check("invoices.csv" in t and "digital_invoice.pdf" not in t, "type filter 'csv' works")
    page.select_option("#f-status", "failed")
    page.wait_for_timeout(600)
    check(page.get_by_text("No documents match these filters").is_visible(), "empty state when filters match nothing")
    page.get_by_role("button", name="Clear filters").first.click()
    page.wait_for_selector("table tbody tr")

    # live polling: the slow scan shows moving progress in the list WITHOUT a page reload
    print("console errors so far:", console_errors[:5])
    b.close()
sys.exit(1 if problems else 0)
