"""RapidOCR (onnxruntime) adapter. Pip-only, no system install. Models load lazily on first use.

RapidOCR returns text *lines*; we split each line into words, dividing the line box by character count
(good enough for click-to-source highlighting; word-exact boxes are an engine-specific upgrade).
"""
import importlib.util
import io
import os

from ...core.interfaces import OCRCaps, OCRPage, OCRWord, ProviderHealth


class RapidOCRProvider:
    name = "rapidocr"

    def __init__(self) -> None:
        self._engine = None

    def capabilities(self) -> OCRCaps:
        return OCRCaps(langs=["eng", "chi_sim"])  # bundled PP-OCR models: English + Chinese

    def health(self) -> ProviderHealth:
        ok = importlib.util.find_spec("rapidocr_onnxruntime") is not None
        return ProviderHealth(ok, "ready" if ok else "pip install rapidocr-onnxruntime")

    def recognize(self, image: bytes, lang: list[str]) -> OCRPage:
        import numpy as np
        from PIL import Image

        if self._engine is None:
            from rapidocr_onnxruntime import RapidOCR
            # onnxruntime defaults to one thread per core; on many-core machines that thrashes badly
            # (measured 3-4 s/page at 1 thread, erratic 3-40 s at 2-22 on a loaded 22-core box). Override with ADSTUDIO_OCR_THREADS.
            threads = int(os.environ.get("ADSTUDIO_OCR_THREADS", "1"))
            self._engine = RapidOCR(intra_op_num_threads=threads, inter_op_num_threads=1)
        img = Image.open(io.BytesIO(image)).convert("RGB")
        W, H = img.size
        result, _ = self._engine(np.array(img))
        words: list[OCRWord] = []
        texts: list[str] = []
        confs: list[float] = []
        for line_no, (box, text, score) in enumerate(result or []):
            xs, ys = [p[0] for p in box], [p[1] for p in box]
            x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
            conf = float(score)
            tokens = text.split()
            if not tokens:
                continue
            texts.append(text)
            confs.append(conf)
            total = sum(len(t) for t in tokens) + max(0, len(tokens) - 1)
            cursor = x0
            for t in tokens:
                w = (x1 - x0) * len(t) / total
                words.append(OCRWord(t, cursor / W, y0 / H, w / W, (y1 - y0) / H, conf, line=line_no))
                cursor += w + (x1 - x0) / total  # + one space
        return OCRPage(words=words, text="\n".join(texts), mean_conf=sum(confs) / len(confs) if confs else 0.0)
