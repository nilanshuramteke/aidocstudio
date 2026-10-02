"""Image enhancement for low-confidence pages: denoise, deskew, adaptive binarize. Only run on demand."""
import cv2
import numpy as np

SKEW_RANGE_DEG = 5.0
SKEW_STEP_DEG = 0.25


def _rotate(gray: np.ndarray, angle: float, border: int = 255) -> np.ndarray:
    h, w = gray.shape
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(gray, m, (w, h), flags=cv2.INTER_LINEAR, borderValue=border)


def estimate_skew(gray: np.ndarray) -> float:
    """Angle (degrees, as accepted by `_rotate`) that best straightens text lines.

    Projection-profile search: the right angle maximizes the variance of row ink sums.
    """
    scale = 600 / max(gray.shape)
    small = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else gray
    ink = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    best, best_score = 0.0, -1.0
    for a in np.arange(-SKEW_RANGE_DEG, SKEW_RANGE_DEG + 1e-6, SKEW_STEP_DEG):
        score = float(np.var(_rotate(ink, a, border=0).sum(axis=1)))
        if score > best_score:
            best, best_score = float(a), score
    return best


def enhance(png: bytes) -> bytes:
    gray = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_GRAYSCALE)
    if gray is None:
        return png
    gray = cv2.fastNlMeansDenoising(gray, None, h=10, templateWindowSize=7, searchWindowSize=21)
    angle = estimate_skew(gray)
    if abs(angle) >= SKEW_STEP_DEG:
        gray = _rotate(gray, angle)
    gray = cv2.adaptiveThreshold(cv2.GaussianBlur(gray, (3, 3), 0), 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                 cv2.THRESH_BINARY, 31, 15)
    ok, out = cv2.imencode(".png", gray)
    return out.tobytes() if ok else png
