"""DocklyOCR Pipeline v4 — port of Anhang C reference code.

This module provides the canonical OCR pipeline used by DocklyOCR. It is
intentionally a 1:1 port of the bewährte v4 multi-strategy pipeline from the
spec's Anhang C, with the following deliberate adjustments for production use:

* `sips` (macOS-only) is replaced by Pillow's ``thumbnail`` for resizing.
* Module-level constants are replaced by ``app.config.settings`` injection.
* HTTP calls go through ``httpx.Client`` (consistent with the rest of the app).
* The pipeline returns an ``OcrResult`` dataclass instead of writing files.

The 13 strategies, split logic and grayscale handling are preserved verbatim
from the reference implementation.
"""

from __future__ import annotations

import base64
import contextlib
import glob
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx
from PIL import Image

from app.config import settings

# ── Public dataclasses ────────────────────────────────────────────────


@dataclass
class PageResult:
    """Result for a single OCR'd page."""

    number: int
    text: str | None  # None when all strategies failed
    strategy: str  # strategy name, or "ALLE_FEHLGESCHLAGEN"
    elapsed_s: float
    is_table: bool = False


@dataclass
class OcrResult:
    """Aggregated result for an entire input document."""

    pages: list[PageResult]
    page_count: int
    pages_ok: int
    pages_failed: int

    def to_json_dict(self) -> dict:
        return {
            "page_count": self.page_count,
            "pages_ok": self.pages_ok,
            "pages_failed": self.pages_failed,
            "pages": [asdict(p) for p in self.pages],
        }

    @classmethod
    def from_json_dict(cls, data: dict) -> OcrResult:
        pages = [
            PageResult(
                number=p["number"],
                text=p["text"],
                strategy=p["strategy"],
                elapsed_s=p["elapsed_s"],
                is_table=p.get("is_table", False),
            )
            for p in data["pages"]
        ]
        return cls(
            pages=pages,
            page_count=data["page_count"],
            pages_ok=data["pages_ok"],
            pages_failed=data["pages_failed"],
        )


# ── Strategies (1:1 from Anhang C) ────────────────────────────────────

# (name, dpi, max_px, grayscale, quality, split)
STRATEGIES: list[tuple[str, int, int, bool, int, bool]] = [
    ("150dpi/1024px", 150, 1024, False, 85, False),
    ("100dpi/768px", 100, 768, False, 85, False),
    ("72dpi/512px", 72, 512, False, 80, False),
    ("150dpi/1024px/gray", 150, 1024, True, 85, False),
    ("100dpi/768px/gray", 100, 768, True, 80, False),
    ("72dpi/512px/gray", 72, 512, True, 75, False),
    ("100dpi/400px/compress", 100, 400, False, 50, False),
    ("72dpi/400px/gray/comp", 72, 400, True, 50, False),
    ("150dpi/split", 150, 1024, False, 80, True),
    ("100dpi/split/gray", 100, 768, True, 75, True),
    ("72dpi/300px/gray/comp", 72, 300, True, 40, False),
    ("150dpi/600px", 150, 600, False, 80, False),
    ("100dpi/600px/gray", 100, 600, True, 75, False),
]

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}
PDF_SUFFIXES = {".pdf"}


# ── Helpers ───────────────────────────────────────────────────────────


def _call_ollama(img_path: Path) -> str:
    """Send image to glm-ocr, return text or raise on error."""
    with open(img_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    with httpx.Client(timeout=settings.ollama_request_timeout_s) as client:
        r = client.post(
            f"{settings.ollama_url.rstrip('/')}/api/generate",
            json={
                "model": settings.ollama_model,
                "prompt": "OCR",
                "images": [b64],
                "stream": False,
            },
        )
        r.raise_for_status()
        return r.json().get("response", "")


def extract_page_image(pdf_path: Path, page_num: int, dpi: int, tmp_dir: Path) -> Path | None:
    """Extract single page via pdftoppm, return image path."""
    prefix = tmp_dir / f"pg{page_num}"
    subprocess.run(
        [
            "pdftoppm",
            "-r",
            str(dpi),
            "-jpeg",
            "-f",
            str(page_num),
            "-l",
            str(page_num),
            str(pdf_path),
            str(prefix),
        ],
        capture_output=True,
        timeout=30,
    )
    candidates = glob.glob(f"{prefix}*.jpg")
    return Path(candidates[0]) if candidates else None


def _resize_image_inplace(img_path: Path, max_px: int) -> None:
    """Resize image in-place via Pillow (replaces macOS-only sips).

    Uses ``Image.thumbnail`` which preserves aspect ratio and only shrinks
    (never enlarges), matching the behaviour of ``sips --resampleHeightWidthMax``.
    """
    img = Image.open(img_path)
    img.thumbnail((max_px, max_px), Image.LANCZOS)
    img.save(img_path, "JPEG", quality=90)


def to_grayscale_jpeg(
    src_path: Path, dst_path: Path, max_px: int | None = None, quality: int = 75
) -> Path:
    """Convert to grayscale JPEG via Pillow, optionally resize."""
    img = Image.open(src_path).convert("L")  # grayscale
    if max_px:
        w, h = img.size
        ratio = min(max_px / w, max_px / h, 1.0)
        if ratio < 1.0:
            img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
    img.save(dst_path, "JPEG", quality=quality)
    return dst_path


def to_compressed_jpeg(
    src_path: Path, dst_path: Path, max_px: int = 400, quality: int = 50
) -> Path:
    """Heavy compression + small size."""
    img = Image.open(src_path).convert("RGB")
    w, h = img.size
    ratio = min(max_px / w, max_px / h, 1.0)
    if ratio < 1.0:
        img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
    img.save(dst_path, "JPEG", quality=quality)
    return dst_path


def split_page_halves(src_path: Path, tmp_dir: Path, page_num: int) -> tuple[Path, Path]:
    """Split image into top and bottom halves."""
    img = Image.open(src_path)
    w, h = img.size
    mid = h // 2
    top = img.crop((0, 0, w, mid))
    bot = img.crop((0, mid, w, h))
    top_path = tmp_dir / f"pg{page_num}_top.jpg"
    bot_path = tmp_dir / f"pg{page_num}_bot.jpg"
    top.save(top_path, "JPEG", quality=80)
    bot.save(bot_path, "JPEG", quality=80)
    return top_path, bot_path


def try_ocr(img_path: Path, label: str = "") -> tuple[str, bool, float]:
    """Try OCR, return (text, ok, elapsed_s)."""
    try:
        t0 = time.time()
        text = _call_ollama(img_path)
        elapsed = time.time() - t0
        if text.strip():
            return text.strip(), True, elapsed
        return "", False, 0.0
    except Exception:
        return "", False, 0.0


def get_page_count(pdf_path: Path) -> int:
    r = subprocess.run(["pdfinfo", str(pdf_path)], capture_output=True, text=True, timeout=10)
    for line in r.stdout.splitlines():
        if line.startswith("Pages:"):
            return int(line.split(":")[1].strip())
    return 0


def _cleanup_page_tmpfiles(tmp_dir: Path, page_num: int) -> None:
    """Remove tmp files matching ``pgN*`` after every strategy attempt."""
    for f in glob.glob(str(tmp_dir / f"pg{page_num}*")):
        with contextlib.suppress(OSError):
            Path(f).unlink()


# ── Strategy loops ────────────────────────────────────────────────────


def _ocr_single_strategy(
    img_path: Path,
    tmp_dir: Path,
    page_num: int,
    name: str,
    max_px: int,
    gray: bool,
    quality: int,
    split: bool,
) -> tuple[str | None, float]:
    """Run a single strategy on an already-extracted page image.

    Returns (text, elapsed_s) on success, or (None, 0.0) on failure.

    The ``img_path`` itself is *not* deleted by this helper — caller is
    responsible for resetting/cleaning up between strategies.
    """
    if not split:
        # Resize (in-place via Pillow, was sips)
        _resize_image_inplace(img_path, max_px)

        # Apply grayscale / compression if needed
        if gray or quality < 80:
            proc_path = tmp_dir / f"pg{page_num}_proc.jpg"
            if gray:
                to_grayscale_jpeg(img_path, proc_path, max_px=max_px, quality=quality)
            else:
                to_compressed_jpeg(img_path, proc_path, max_px=max_px, quality=quality)
            target = proc_path
        else:
            target = img_path

        text, ok, elapsed = try_ocr(target)
        if ok:
            return text, elapsed
        return None, 0.0

    # Split strategy: OCR top half + bottom half separately
    _resize_image_inplace(img_path, max_px)

    if gray:
        gray_path = tmp_dir / f"pg{page_num}_gray.jpg"
        to_grayscale_jpeg(img_path, gray_path, max_px=max_px, quality=quality)
        source = gray_path
    else:
        source = img_path

    top_path, bot_path = split_page_halves(source, tmp_dir, page_num)

    top_text, top_ok, _ = try_ocr(top_path)
    bot_text, bot_ok, _ = try_ocr(bot_path)

    if top_ok or bot_ok:
        combined = ""
        if top_ok:
            combined += top_text
        if bot_ok:
            combined += "\n" + bot_text
        return combined.strip(), 0.0

    return None, 0.0


def _ocr_image_with_strategies(src_image: Path, tmp_dir: Path, page_num: int) -> PageResult:
    """Run all 13 strategies against a *single* image input (no PDF extraction).

    Used when the input is already an image (jpg/png/tiff). Each strategy gets
    a fresh working copy of the source image so that the in-place resize never
    modifies the caller's file.
    """
    for name, _dpi, max_px, gray, quality, split in STRATEGIES:
        # Always operate on a fresh copy — never mutate the caller's source.
        work_path = tmp_dir / f"pg{page_num}.jpg"
        try:
            img = Image.open(src_image).convert("RGB")
            img.save(work_path, "JPEG", quality=95)
        except Exception:
            _cleanup_page_tmpfiles(tmp_dir, page_num)
            continue

        text, elapsed = _ocr_single_strategy(
            work_path, tmp_dir, page_num, name, max_px, gray, quality, split
        )

        _cleanup_page_tmpfiles(tmp_dir, page_num)

        if text is not None:
            return PageResult(number=page_num, text=text, strategy=name, elapsed_s=elapsed)

    return PageResult(number=page_num, text=None, strategy="ALLE_FEHLGESCHLAGEN", elapsed_s=0.0)


def _ocr_pdf_page(pdf_path: Path, page_num: int, tmp_dir: Path) -> PageResult:
    """Try all strategies for a PDF page until one succeeds."""
    for name, dpi, max_px, gray, quality, split in STRATEGIES:
        # 1) Extract page image at the strategy's DPI
        img_path = extract_page_image(pdf_path, page_num, dpi, tmp_dir)
        if img_path is None:
            _cleanup_page_tmpfiles(tmp_dir, page_num)
            continue

        text, elapsed = _ocr_single_strategy(
            img_path, tmp_dir, page_num, name, max_px, gray, quality, split
        )

        _cleanup_page_tmpfiles(tmp_dir, page_num)

        if text is not None:
            return PageResult(number=page_num, text=text, strategy=name, elapsed_s=elapsed)

    return PageResult(number=page_num, text=None, strategy="ALLE_FEHLGESCHLAGEN", elapsed_s=0.0)


# ── Public entry point ────────────────────────────────────────────────


def run_ocr(input_path: Path, tmp_dir: Path) -> OcrResult:
    """Run the 13-strategy OCR pipeline on a PDF or image input.

    Args:
        input_path: Path to PDF (`.pdf`) or image (`.jpg/.jpeg/.png/.tif/.tiff`).
        tmp_dir: Working directory for intermediate files. Must already exist
            (caller's responsibility); this function only writes inside it and
            cleans up its own ``pgN*`` files between strategies.

    Returns:
        ``OcrResult`` with one ``PageResult`` per page. Failed pages have
        ``text=None`` and ``strategy="ALLE_FEHLGESCHLAGEN"`` — no exception
        is raised; the caller decides how to render failures.
    """
    input_path = Path(input_path)
    tmp_dir = Path(tmp_dir)
    suffix = input_path.suffix.lower()

    pages: list[PageResult] = []

    if suffix in PDF_SUFFIXES:
        page_count = get_page_count(input_path)
        for pg in range(1, page_count + 1):
            pages.append(_ocr_pdf_page(input_path, pg, tmp_dir))
    elif suffix in IMAGE_SUFFIXES:
        pages.append(_ocr_image_with_strategies(input_path, tmp_dir, page_num=1))
    else:
        raise ValueError(
            f"Unsupported input type: {suffix!r} (expected PDF or one of {sorted(IMAGE_SUFFIXES)})"
        )

    page_count = len(pages)
    pages_ok = sum(1 for p in pages if p.text is not None and p.text.strip() != "")
    pages_failed = page_count - pages_ok

    return OcrResult(
        pages=pages,
        page_count=page_count,
        pages_ok=pages_ok,
        pages_failed=pages_failed,
    )
