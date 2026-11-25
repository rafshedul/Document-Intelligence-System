"""Browser test: sign-in, phone-sized screens, error states.
Expects a SECOND stack for the sign-in part: a production-mode API on :8001 with API_AUTH_TOKEN=prod-token-0123456789abcdef
and the frontend built with NEXT_PUBLIC_API_URL=http://localhost:8001, served on :3001 (see tests/e2e/README.md).
It stops the normal API on :8000 at the end, to check the backend-down message.
"""
import os
import re, subprocess, sys, time, urllib.request, json
from playwright.sync_api import sync_playwright, expect
SHOTS=os.environ.get("E2E_SHOTS", "/tmp/docintel-e2e/shots"); problems=[]
def check(c,m):
    print(("PASS " if c else "FAIL ")+m)
    if not c: problems.append(m)
def api(path, method="GET", token=None):
    req=urllib.request.Request("http://localhost:8000"+path, method=method)
    return urllib.request.urlopen(req)

with sync_playwright() as p:
    b=p.chromium.launch()

    # ---------- login (production-mode API on :8001, frontend build on :3001) ----------
    ctx=b.new_context(viewport={"width":1280,"height":800}); page=ctx.new_page()
    page.goto("http://localhost:3001/documents")
    expect(page.get_by_role("heading", name="Sign in")).to_be_visible()
    check(True, "a protected API shows the sign-in box instead of the app")
    page.screenshot(path=f"{SHOTS}/10_login.png")
    page.fill("#token","wrong-token"); page.get_by_role("button", name="Sign in").click()
    page.wait_for_selector("text=That token is not right")
    check(page.get_by_role("heading", name="Sign in").is_visible(), "wrong token: message, still on the sign-in box")
    check(page.evaluate("sessionStorage.getItem('docintel_token')") is None, "wrong token is not kept")
    page.fill("#token","prod-token-0123456789abcdef"); page.get_by_role("button", name="Sign in").click()
    page.wait_for_selector("h1:has-text('Documents')")
    check(True, "right token: the app opens")
    page.reload(); page.wait_for_selector("h1:has-text('Documents')")
    check(True, "a refresh keeps you signed in (token kept for the tab)")
    page.get_by_role("button", name="Sign out").click()
    expect(page.get_by_role("heading", name="Sign in")).to_be_visible()
    check(True, "sign out returns to the sign-in box")
    page.fill("#token","prod-token-0123456789abcdef"); page.get_by_role("button", name="Sign in").click(); page.wait_for_selector("h1:has-text('Documents')")
    page.evaluate("sessionStorage.setItem('docintel_token','revoked')")   # token stops working while you are using the app
    page.get_by_role("link", name="Search").click(); page.fill("#q","x"); page.get_by_role("button", name="Search", exact=True).click()
    expect(page.get_by_role("heading", name="Sign in")).to_be_visible()
    check(True, "a token that stops working sends you back to sign in (no broken page)")
    ctx.close()

    # ---------- phone-sized screens ----------
    ctx=b.new_context(viewport={"width":390,"height":844}, device_scale_factor=2, is_mobile=True, has_touch=True); page=ctx.new_page()
    def no_overflow(name):
        w=page.evaluate("[document.documentElement.scrollWidth, window.innerWidth]")
        check(w[0] <= w[1]+1, f"mobile: {name} has no sideways scrolling (page {w[0]}px, screen {w[1]}px)")
    page.goto(BASE + "/documents"); page.wait_for_selector("ul.md\\:hidden li")
    check(page.locator("ul.md\\:hidden li").first.is_visible() and not page.locator("table").is_visible(), "mobile: list shows cards, not the wide table")
    no_overflow("documents"); page.screenshot(path=f"{SHOTS}/11_mobile_documents.png")
    page.goto(BASE + "/upload"); page.wait_for_selector("text=Drop files here"); no_overflow("upload"); page.screenshot(path=f"{SHOTS}/12_mobile_upload.png")
    page.goto(BASE + "/search?q=hosting&mode=keyword"); page.wait_for_selector("li.card"); no_overflow("search"); page.screenshot(path=f"{SHOTS}/13_mobile_search.png")
    page.goto(BASE + "/documents"); page.wait_for_selector("ul.md\\:hidden li")
    page.locator("ul.md\\:hidden a", has_text="digital_invoice.pdf").click(); page.wait_for_selector("#field-total")
    no_overflow("detail"); page.screenshot(path=f"{SHOTS}/14_mobile_detail.png", full_page=True)
    btn=page.get_by_role("button", name="Hide original file"); check(btn.is_visible(), "mobile: original file can be hidden to save space")
    btn.click(); check(page.get_by_role("button", name="Show original file").is_visible(), "mobile: ...and shown again")
    page.locator("#field-total [role=button]").tap(); check(page.locator("#field-total input").is_visible(), "mobile: tapping a value opens the editor")
    no_overflow("detail in edit mode"); page.screenshot(path=f"{SHOTS}/15_mobile_edit.png")
    ctx.close()

    # ---------- errors and empty states ----------
    ctx=b.new_context(viewport={"width":1280,"height":800}); page=ctx.new_page()
    page.goto(BASE + "/documents/00000000-0000-0000-0000-000000000000")
    page.wait_for_selector("text=This document does not exist")
    check(page.get_by_role("link", name="← Back to documents").is_visible(), "unknown document: clear message and a way back")
    page.goto(BASE + "/documents/not-a-uuid"); page.wait_for_selector("[role=alert]")
    check(True, "malformed id: an error message, not a crash")

    # backend goes away
    page.goto(BASE + "/documents"); page.wait_for_selector("table tbody tr")
    subprocess.run(["pkill","-f","uvicorn app.main:app --port 8000"]); time.sleep(1.5)
    page.reload()
    page.wait_for_selector("text=Cannot load the app", timeout=15000)
    check(page.get_by_text("Cannot reach the server").is_visible() and page.get_by_role("button", name="Try again").is_visible(), "backend down: clear message and a Try again button")
    page.screenshot(path=f"{SHOTS}/16_backend_down.png")
    b.close()
sys.exit(1 if problems else 0)
