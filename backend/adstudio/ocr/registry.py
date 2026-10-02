"""Engine registry + capability probe. Concrete adapters are only referenced here."""
from ..core.interfaces import OCRProvider

ENGINES = ("rapidocr", "tesseract")  # preference order for engine="auto"; revisit after the Phase 2 eval


def make_provider(name: str) -> OCRProvider:
    if name == "rapidocr":
        from .providers.rapid import RapidOCRProvider
        return RapidOCRProvider()
    if name == "tesseract":
        from .providers.tesseract import TesseractProvider
        return TesseractProvider()
    raise ValueError(f"unknown OCR engine: {name}")


def choose_engine(preferred: str = "auto") -> str | None:
    """Return the first healthy engine (or the preferred one if healthy), else None."""
    order = ENGINES if preferred == "auto" else (preferred,) + tuple(e for e in ENGINES if e != preferred)
    for name in order:
        try:
            if make_provider(name).health().ok:
                return name
        except Exception:  # noqa: BLE001
            continue
    return None
