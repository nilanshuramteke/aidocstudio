"""Regenerate the README screenshots and demo GIF from a REAL running app and the fake files in samples/.

Needs (not project dependencies):  pip install playwright httpx pillow   (uses the installed Microsoft Edge/Chrome)
Run from the repo root:            python scripts/capture_screenshots.py
Uses a temp data folder. Ollama should be running if you want the Ask AI frame to contain an answer.
"""
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "images"
SIZE = {"width": 1360, "height": 820}


def launch():
    data = tempfile.mkdtemp(prefix="adstudio-shots-")
    venv_py = ROOT / "backend" / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    proc = subprocess.Popen([str(venv_py if venv_py.exists() else sys.executable), "-m", "adstudio.main", "--data-dir", data, "--no-browser"], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, cwd=ROOT / "backend")
    t0 = time.time()
    while time.time() - t0 < 60:
        m = re.search(r"Open: (http://\S+)", proc.stdout.readline())
        if m:
            return proc, m.group(1)
    raise SystemExit("server did not start")


def import_samples(base: str, cookies) -> None:
    c = httpx.Client(base_url=base, cookies=cookies, timeout=60)
    files = [("files", (p.name, p.read_bytes())) for p in sorted((ROOT / "samples").iterdir())]
    ids = [r["document_id"] for r in c.post("/api/v1/documents/import", files=files).json()["results"]]
    deadline = time.time() + 300
    while time.time() < deadline:
        if all(c.get(f"/api/v1/documents/{i}").json()["state"] in ("ready", "needs_review", "failed") for i in ids):
            return
        time.sleep(2)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    proc, url = launch()
    base = url.split("/?")[0]
    frames: list[Path] = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="msedge")
            ctx = browser.new_context(viewport=SIZE, device_scale_factor=1, color_scheme="light")
            page = ctx.new_page()
            page.goto(url)
            page.wait_for_selector(".rail")
            import_samples(base, {c["name"]: c["value"] for c in ctx.cookies()})

            def shot(name: str, gif: bool = True) -> None:
                page.wait_for_timeout(700)
                p = OUT / f"{name}.png"
                page.screenshot(path=str(p))
                if gif:
                    frames.append(p)

            def nav(label: str) -> None:
                page.locator(".nav button", has_text=label).click()

            page.reload(); page.wait_for_selector(".hero"); shot("home")
            nav("Documents"); page.wait_for_selector("table"); shot("documents")
            page.locator("tbody td button", has_text=re.compile("abc", re.I)).first.click()
            page.wait_for_timeout(3000); shot("document-detail")
            nav("Search"); page.get_by_label("Search", exact=True).fill("invoices above 50,000")
            page.keyboard.press("Enter"); page.wait_for_timeout(1500); shot("search")
            nav("Review"); page.wait_for_timeout(1500); shot("review")
            nav("Ask AI"); page.get_by_label("Ask a question").fill("How much notice is needed to end the lease?")
            page.keyboard.press("Enter")
            try:
                page.wait_for_selector("text=/ninety|Not found/i", timeout=120000)
                page.get_by_role("button", name="Stop").wait_for(state="detached", timeout=120000)
            except Exception:
                pass
            shot("ask-ai")
            page.keyboard.press("Control+k"); page.wait_for_selector(".palette"); shot("command-palette", gif=False)
            page.keyboard.press("Escape")
            state = ctx.storage_state()
            ctx.close()
            dark = browser.new_context(viewport=SIZE, color_scheme="dark", storage_state=state)
            dp = dark.new_page(); dp.goto(base + "/"); dp.wait_for_selector(".hero"); dp.wait_for_timeout(800)
            dp.screenshot(path=str(OUT / "home-dark.png"))
            browser.close()
    finally:
        proc.terminate()
    imgs = [Image.open(f).convert("RGB").resize((960, 580), Image.LANCZOS) for f in frames]
    if imgs:
        imgs[0].save(OUT / "demo.gif", save_all=True, append_images=imgs[1:], duration=1800, loop=0, optimize=True)
    print("wrote", sorted(p.name for p in OUT.iterdir()))


if __name__ == "__main__":
    main()
