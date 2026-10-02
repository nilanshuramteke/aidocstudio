"""Tesseract 5 adapter (needs the Tesseract binary + language packs installed on the system)."""
import io
import shutil

from ...core.interfaces import OCRCaps, OCRPage, OCRWord, ProviderHealth

_WIN_DEFAULT = r"C:\Program Files\Tesseract-OCR\tesseract.exe"


def _binary() -> str | None:
    import os
    return shutil.which("tesseract") or (_WIN_DEFAULT if os.path.exists(_WIN_DEFAULT) else None)


class TesseractProvider:
    name = "tesseract"

    def capabilities(self) -> OCRCaps:
        try:
            import pytesseract
            pytesseract.pytesseract.tesseract_cmd = _binary() or "tesseract"
            return OCRCaps(langs=sorted(set(pytesseract.get_languages()) - {"osd"}))
        except Exception:  # noqa: BLE001
            return OCRCaps(langs=[])

    def health(self) -> ProviderHealth:
        b = _binary()
        return ProviderHealth(b is not None, b or "Tesseract binary not found; install from UB-Mannheim builds")

    def recognize(self, image: bytes, lang: list[str]) -> OCRPage:
        import pytesseract
        from PIL import Image

        pytesseract.pytesseract.tesseract_cmd = _binary() or "tesseract"
        img = Image.open(io.BytesIO(image)).convert("RGB")
        W, H = img.size
        d = pytesseract.image_to_data(img, lang="+".join(lang) or "eng", output_type=pytesseract.Output.DICT)
        words, lines, confs = [], {}, []
        for i, t in enumerate(d["text"]):
            if not t.strip() or float(d["conf"][i]) < 0:
                continue
            c = float(d["conf"][i]) / 100
            confs.append(c)
            key = (d["block_num"][i], d["par_num"][i], d["line_num"][i])
            lines.setdefault(key, []).append(t)
            words.append(OCRWord(t, d["left"][i] / W, d["top"][i] / H, d["width"][i] / W, d["height"][i] / H, c,
                                 line=d["line_num"][i], block=d["block_num"][i]))
        return OCRPage(words=words, text="\n".join(" ".join(v) for v in lines.values()),
                       mean_conf=sum(confs) / len(confs) if confs else 0.0)
