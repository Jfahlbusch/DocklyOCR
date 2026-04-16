"""DocklyOCR Pipeline v5 — port of Anhang C reference code.

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
import re
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


def _batch_extract_pages(pdf_path: Path, tmp_dir: Path, dpi: int = 150) -> list[Path]:
    """Extract ALL pages at once via pdftoppm. Returns sorted list of JPEG paths."""
    prefix = tmp_dir / "page"
    subprocess.run(
        ["pdftoppm", "-r", str(dpi), "-jpeg", str(pdf_path), str(prefix)],
        capture_output=True,
        timeout=120,
        check=True,
    )
    return sorted(tmp_dir.glob("page-*.jpg"))


def _downscale_for_strategy(src_150dpi: Path, target_dpi: int, tmp_dir: Path) -> Path:
    """Proportional downscale from 150dpi source to target_dpi."""
    scale = target_dpi / 150
    img = Image.open(src_150dpi)
    new_size = (int(img.width * scale), int(img.height * scale))
    img = img.resize(new_size, Image.LANCZOS)
    out = tmp_dir / f"scaled_{target_dpi}dpi_{src_150dpi.name}"
    img.save(out, "JPEG", quality=90)
    return out


# ── Helpers ───────────────────────────────────────────────────────────

TABLE_INDICATORS: list[str] = [
    r"\|.*\|.*\|",  # at least 3 pipe-delimited columns
    r"\d+[.,]\d{2}\s+\d+",  # numeric columns (e.g. 1.234,56  234)
    r"[-–]{3,}\s*\+",  # horizontal rules with crosses
]


def _detect_table_patterns(text: str) -> bool:
    """Heuristic: True if >= 30% of lines match table indicators."""
    lines = text.strip().splitlines()
    if len(lines) < 3:
        return False
    matches = sum(1 for line in lines if any(re.search(pat, line) for pat in TABLE_INDICATORS))
    return matches / len(lines) >= 0.3


def _html_table_to_markdown(html: str) -> str:
    """Convert an HTML table to Markdown table format."""
    if "<table" not in html.lower() and "<tr" not in html.lower():
        return html
    lines: list[str] = []
    # Extract rows
    row_pattern = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
    cell_pattern = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.DOTALL | re.IGNORECASE)
    rows = row_pattern.findall(html)
    for i, row_html in enumerate(rows):
        cells = cell_pattern.findall(row_html)
        # Strip nested HTML tags from cell content
        clean_cells = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
        lines.append("| " + " | ".join(clean_cells) + " |")
        if i == 0:
            lines.append("| " + " | ".join("---" for _ in clean_cells) + " |")
    return "\n".join(lines) if lines else html


def _ocr_table(img_path: Path) -> str:
    """Re-OCR with table-specific prompt. Returns Markdown table text."""
    with open(img_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    with httpx.Client(timeout=settings.ollama_request_timeout_s) as client:
        r = client.post(
            f"{settings.ollama_url.rstrip('/')}/api/generate",
            json={
                "model": settings.ollama_model,
                "prompt": (
                    "Extract all tables from this image as Markdown tables "
                    "using | delimiters. Keep headers. Output ONLY the table, "
                    "no explanation."
                ),
                "images": [b64],
                "stream": False,
            },
        )
        r.raise_for_status()
        return r.json().get("response", "").strip()


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


def split_page_columns(src_path: Path, tmp_dir: Path, page_num: int) -> tuple[Path, Path]:
    """Split image into left and right halves (for multi-column layouts)."""
    img = Image.open(src_path)
    w, h = img.size
    mid = w // 2
    left = img.crop((0, 0, mid, h))
    right = img.crop((mid, 0, w, h))
    left_path = tmp_dir / f"pg{page_num}_left.jpg"
    right_path = tmp_dir / f"pg{page_num}_right.jpg"
    left.save(left_path, "JPEG", quality=80)
    right.save(right_path, "JPEG", quality=80)
    return left_path, right_path


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


MAX_STRATEGIES: int = 5  # Only try the first N strategies (rest rarely help, burn timeout)


def _ocr_image_with_strategies(src_image: Path, tmp_dir: Path, page_num: int) -> PageResult:
    """Run strategies against a *single* image input (no PDF extraction).

    Tries at most ``MAX_STRATEGIES`` (default 5) before giving up.
    Used when the input is already an image (jpg/png/tiff). Each strategy gets
    a fresh working copy of the source image so that the in-place resize never
    modifies the caller's file.
    """
    for name, _dpi, max_px, gray, quality, split in STRATEGIES[:MAX_STRATEGIES]:
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

    # Last resort: try left/right column split (for multi-column layouts)
    try:
        work_path = tmp_dir / f"pg{page_num}.jpg"
        img = Image.open(src_image).convert("RGB")
        img.save(work_path, "JPEG", quality=95)
        _resize_image_inplace(work_path, 1024)
        left_path, right_path = split_page_columns(work_path, tmp_dir, page_num)
        # Do NOT cleanup here — left/right images are still needed for OCR
        work_path.unlink(missing_ok=True)

        left_text, left_ok, _ = try_ocr(left_path)
        right_text, right_ok, _ = try_ocr(right_path)

        # Cleanup split images after OCR
        for f in [left_path, right_path]:
            with contextlib.suppress(OSError):
                f.unlink()

        if left_ok or right_ok:
            combined = ""
            if left_ok:
                combined += left_text
            if right_ok:
                combined += "\n\n" + right_text
            return PageResult(
                number=page_num, text=combined.strip(), strategy="column-split", elapsed_s=0.0
            )
    except Exception:
        _cleanup_page_tmpfiles(tmp_dir, page_num)

    return PageResult(number=page_num, text=None, strategy="ALLE_FEHLGESCHLAGEN", elapsed_s=0.0)


def _ocr_pdf_page(pdf_path: Path, page_num: int, tmp_dir: Path) -> PageResult:
    # v4 legacy — retained for direct use and existing tests
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


# ── Incremental writer ────────────────────────────────────────────────


class IncrementalWriter:
    """Appends formatted page chunks to an output file during processing."""

    def __init__(self, output_path: Path, fmt: str) -> None:
        self.output_path = output_path
        self.fmt = fmt
        self.output_path.write_bytes(b"")  # truncate / create

    def append_chunk(self, pages: list[PageResult]) -> None:
        """Format and append a chunk of pages. No-op for json/toon."""
        if self.fmt not in ("md", "txt"):
            return
        with open(self.output_path, "a", encoding="utf-8") as f:
            for page in pages:
                if page.text is None:
                    if self.fmt == "md":
                        f.write(f"## Seite {page.number}\n\n")
                        f.write(f"[OCR-Fehler auf Seite {page.number}]\n\n")
                    elif self.fmt == "txt":
                        f.write(f"[OCR-Fehler Seite {page.number}]\f")
                    continue
                if self.fmt == "md":
                    f.write(f"## Seite {page.number}\n\n")
                    f.write(page.text + "\n\n")
                    if not page.is_table:
                        f.write(f"> OCR-Strategie: `{page.strategy}`\n\n")
                elif self.fmt == "txt":
                    f.write(page.text + "\f")

    def finalize(self, result: OcrResult) -> bytes:
        """For json/toon: write the full document now. Returns final bytes."""
        if self.fmt in ("md", "txt"):
            return self.output_path.read_bytes()
        from app.services.formatters import format_output

        body, _ = format_output(result, self.fmt)
        self.output_path.write_bytes(body)
        return body


# ── Public entry point ────────────────────────────────────────────────


def _is_pdf(path: Path) -> bool:
    return path.suffix.lower() == ".pdf"


def run_ocr(
    input_path: Path,
    tmp_dir: Path,
    output_path: Path | None = None,
    output_format: str = "md",
    pages_dir: Path | None = None,
) -> OcrResult:
    """Public entry point for the OCR pipeline (v5).

    If *output_path* is provided, results are written incrementally
    for ``md`` and ``txt`` formats.  ``json`` and ``toon`` are written
    in full at the end via :meth:`IncrementalWriter.finalize`.
    """
    input_path = Path(input_path)
    tmp_dir = Path(tmp_dir)
    suffix = input_path.suffix.lower()

    if suffix not in PDF_SUFFIXES and suffix not in IMAGE_SUFFIXES:
        raise ValueError(
            f"Unsupported input type: {suffix!r} (expected PDF or one of {sorted(IMAGE_SUFFIXES)})"
        )

    # 1. Extract pages
    page_images = _batch_extract_pages(input_path, tmp_dir) if _is_pdf(input_path) else [input_path]

    # 1b. Persist page images to pages_dir (visible during processing)
    if pages_dir and _is_pdf(input_path) and page_images:
        import shutil

        pages_dir.mkdir(parents=True, exist_ok=True)
        for img in page_images:
            shutil.copy2(img, pages_dir / img.name)

    if not page_images:
        return OcrResult(pages=[], page_count=0, pages_ok=0, pages_failed=0)

    # 2. Writer (optional)
    writer = IncrementalWriter(output_path, output_format) if output_path else None

    # 3. OCR + table detection per page
    all_pages: list[PageResult] = []
    for i, img_path in enumerate(page_images):
        page_num = i + 1
        page_result = _ocr_image_with_strategies(img_path, tmp_dir, page_num)

        # Two-pass table detection
        if page_result.text and _detect_table_patterns(page_result.text):
            try:
                table_text = _ocr_table(img_path)
                if table_text.strip():
                    # Convert HTML tables to Markdown if needed
                    table_text = _html_table_to_markdown(table_text)
                    page_result.text = table_text
                    page_result.is_table = True
            except Exception:
                pass  # keep original text on table-OCR failure

        all_pages.append(page_result)

        # 4. Merge boundary with previous page + write immediately
        if len(all_pages) >= 2:
            _merge_across_boundaries(all_pages[-2:])
        if writer:
            writer.append_chunk([page_result])

    # 6. Build result
    pages_ok = sum(1 for p in all_pages if p.text)
    ocr_result = OcrResult(
        pages=all_pages,
        page_count=len(all_pages),
        pages_ok=pages_ok,
        pages_failed=len(all_pages) - pages_ok,
    )

    # 7. Finalize (json/toon written here)
    if writer:
        writer.finalize(ocr_result)

    return ocr_result


# ── Cross-page boundary helpers ───────────────────────────────────────


def _ends_sentence(line: str) -> bool:
    """True if line ends with terminal punctuation."""
    return bool(line) and line[-1] in '.!?:;»"'


def _starts_new_section(line: str) -> bool:
    """True if line starts a new logical section (§, heading, list marker)."""
    return bool(re.match(r"^(§\s*\d|[A-Z]{2,}|\d+\.\s|[-–•])\s", line))


def _merge_across_boundaries(pages: list[PageResult]) -> list[PageResult]:
    """Fix sentence breaks across page boundaries. Modifies pages in-place."""
    if len(pages) < 2:
        return pages
    for i in range(len(pages) - 1):
        curr = pages[i]
        nxt = pages[i + 1]
        if curr.text is None or nxt.text is None:
            continue
        if curr.is_table or nxt.is_table:
            continue
        last_line = curr.text.rstrip().rsplit("\n", 1)[-1].rstrip()
        first_line = nxt.text.lstrip().split("\n", 1)[0].lstrip()
        if not _ends_sentence(last_line) and not _starts_new_section(first_line):
            curr_parts = curr.text.rstrip().rsplit("\n", 1)
            nxt_parts = nxt.text.lstrip().split("\n", 1)
            if len(curr_parts) == 2:
                curr.text = curr_parts[0]
            else:
                curr.text = ""
            joined = last_line + " " + first_line
            if len(nxt_parts) == 2:
                nxt.text = joined + "\n" + nxt_parts[1]
            else:
                nxt.text = joined
    return pages
