"""OCR eval: synthetic pages in three quality tiers, CER/WER per engine. `python evals/ocr/run.py`.

Synthetic only (no real user documents). Gold text is known by construction. Output: table + evals/ocr/results.json.
Baselines for CI gates are set from a run of this script, not invented (see BLUEPRINT section 25).
"""
import io
import json
import random
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from adstudio.ocr.registry import ENGINES, make_provider

LINES = [
    "Invoice No INV-20491 dated 12 March 2026",
    "Vendor ABC Private Limited Mumbai",
    "GSTIN 27ABCDE1234F1Z5 PAN ABCDE1234F",
    "Total amount payable 54,300.00 INR",
    "Payment terms net thirty days from receipt",
    "This agreement is made between the parties",
    "Termination requires ninety days written notice",
    "Receipt for services rendered during April",
]


def font(size=34):
    for name in ("arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default(size=size)


def render(lines: list[str], tier: str, rng: random.Random) -> Image.Image:
    img = Image.new("L", (1000, 70 * len(lines) + 40), 255)
    d = ImageDraw.Draw(img)
    for i, t in enumerate(lines):
        d.text((30, 25 + i * 70), t, fill=0, font=font())
    if tier == "noisy":
        arr = np.array(img.filter(ImageFilter.GaussianBlur(1.1))).astype(np.float32)
        arr += np.random.default_rng(rng.randint(0, 10**6)).normal(0, 22, arr.shape)
        img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    elif tier == "skewed":
        img = img.rotate(rng.choice([-3, 2.5, 3.5]), fillcolor=255).filter(ImageFilter.GaussianBlur(0.6))
    return img


def levenshtein(a, b) -> int:
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def norm(s: str) -> str:
    return " ".join(s.lower().split())


def main() -> None:
    rng = random.Random(7)
    pages = [(tier, rng.sample(LINES, 4)) for tier in ("clean", "noisy", "skewed") for _ in range(4)]
    results: dict = {}
    for engine in ENGINES:
        try:
            p = make_provider(engine)
            if not p.health().ok:
                results[engine] = {"skipped": p.health().detail}
                continue
        except Exception as e:  # noqa: BLE001
            results[engine] = {"skipped": str(e)}
            continue
        agg: dict[str, dict] = {}
        for tier, lines in pages:
            buf = io.BytesIO()
            render(lines, tier, rng).convert("RGB").save(buf, "PNG")
            t0 = time.perf_counter()
            out = p.recognize(buf.getvalue(), ["eng"])
            dt = time.perf_counter() - t0
            gold, hyp = norm(" ".join(lines)), norm(out.text.replace("\n", " "))
            a = agg.setdefault(tier, {"ce": 0, "cn": 0, "we": 0, "wn": 0, "secs": 0.0, "n": 0})
            a["ce"] += levenshtein(gold, hyp); a["cn"] += len(gold)
            a["we"] += levenshtein(gold.split(), hyp.split()); a["wn"] += len(gold.split())
            a["secs"] += dt; a["n"] += 1
        results[engine] = {t: {"CER": round(a["ce"] / a["cn"], 4), "WER": round(a["we"] / a["wn"], 4),
                               "sec_per_page": round(a["secs"] / a["n"], 2)} for t, a in agg.items()}
    print(f"{'engine':<10} {'tier':<8} {'CER':>7} {'WER':>7} {'s/page':>7}")
    for eng, r in results.items():
        if "skipped" in r:
            print(f"{eng:<10} skipped: {r['skipped']}")
            continue
        for tier, m in r.items():
            print(f"{eng:<10} {tier:<8} {m['CER']:>7.3f} {m['WER']:>7.3f} {m['sec_per_page']:>7.2f}")
    out = Path(__file__).with_name("results.json")
    out.write_text(json.dumps(results, indent=2))
    print(f"wrote {out}")


if __name__ == "__main__":
    sys.exit(main())
