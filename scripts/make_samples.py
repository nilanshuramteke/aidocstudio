"""Generate a small set of FAKE sample documents into samples/ (invoices, a scanned receipt, a lease, a policy).
`python scripts/make_samples.py`. All names, numbers and IDs are invented."""
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = Path(__file__).resolve().parent.parent / "samples"


def pdf(name: str, pages: list[str]) -> None:
    d = pymupdf.open()
    for text in pages:
        p = d.new_page(width=595, height=842)
        y = 70
        for line in text.splitlines():
            big = line.isupper() and line.strip()
            p.insert_text((55, y), line, fontsize=15 if big else 11, fontname="hebo" if big else "helv")
            y += 26 if big else 18
    d.save(OUT / name)


def font(size: int):
    for f in ("arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            pass
    return ImageFont.load_default(size=size)


def scan(name: str, lines: list[str]) -> None:
    img = Image.new("RGB", (900, 120 + 46 * len(lines)), (250, 248, 242))
    d = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        d.text((60, 50 + i * 46), line, fill=(25, 25, 25), font=font(32 if i else 40))
    img.rotate(1.2, expand=True, fillcolor=(250, 248, 242)).filter(ImageFilter.GaussianBlur(0.6)).save(OUT / name)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    pdf("invoice-abc-traders.pdf", ["""TAX INVOICE
ABC Private Limited
Invoice No: INV-20491
Invoice Date: 12/03/2026
GSTIN: 27ABCDE1234F1ZV
Bill To: Acme Traders, Pune

Item                          Qty     Rate        Amount
Office chairs                 10      3,200.00    32,000.00
Standing desks                 2      7,000.00    14,000.00

Subtotal: 46,000.00
GST 18%: 8,280.00
Total Amount Payable: 54,280.00
Payment is due within thirty days."""])
    pdf("invoice-sharma-supplies.pdf", ["""INVOICE
Sharma Office Supplies
Invoice No: SOS/2026/0087
Date: 5 January 2026
Bill To: Acme Traders, Pune

Printer paper (20 reams)        4,000.00
Toner cartridges (4)            9,600.00

Subtotal: 13,600.00
GST 18%: 2,448.00
Grand Total: 16,048.00
Payment terms: Net 15 days."""])
    scan("receipt-cafe-mocha.png", ["CAFE MOCHA ROASTERS", "Thank you for dining with us", "Coffee x2        800.00", "Cake             450.00", "Total          1,250.00", "Paid by card"])
    pdf("lease-agreement.pdf", ["""RESIDENTIAL LEASE AGREEMENT
This agreement is made on 1 April 2026 between Mr. R. Iyer (Landlord) and Ms. P. Menon (Tenant).
The tenant shall pay monthly rent of 45,000 on the first day of each month.
A refundable security deposit of 135,000 is payable on signing.
The lease term is eleven months from the start date.""", """TERMINATION AND LAW
Either party may end this lease with ninety days written notice.
The tenant is responsible for minor repairs below 2,000.
This agreement is governed by the laws of Maharashtra."""])
    pdf("warranty-terms.pdf", ["""PRODUCT WARRANTY TERMS
The product is covered for two years from the date of purchase.
Batteries are covered for six months.
Damage caused by water or accidental drops is not covered.
Claims require the original invoice."""])
    pdf("leave-policy.pdf", ["""EMPLOYEE LEAVE POLICY
Employees receive twenty four days of paid leave per year.
Unused leave up to ten days may be carried forward.
Sick leave requires a medical certificate after three days."""])
    print(f"wrote {len(list(OUT.iterdir()))} files to {OUT}")


if __name__ == "__main__":
    main()
