"""DocklyOCR OCR pipeline.

Entry point: :func:`run_ocr` — takes a PDF or image and returns an
``OcrResult`` with per-page text. The pipeline:

1. Extracts all PDF pages upfront via ``pdftoppm`` (single subprocess call).
2. Processes pages in parallel (``MAX_PARALLEL_PAGES``) against the OCR
   backend (vLLM with Qwen2.5-VL via OpenAI-compatible chat completions).
3. For each page, tries up to ``MAX_STRATEGIES`` render strategies
   (DPI/resize/grayscale combinations) and falls back to a left/right
   column split for multi-column layouts.
4. Optionally runs a second pass with a table-specific prompt for pages
   whose text looks like a table.
5. Merges across page boundaries to recover sentences split over pages.
6. Writes incrementally into ``md``/``txt`` output (or all at once for
   ``json``/``toon``) via :class:`IncrementalWriter`.
"""

from __future__ import annotations

import base64
import contextlib
import glob
import logging
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx
from PIL import Image

from app.config import settings

logger = logging.getLogger(__name__)

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


# ── Strategies ────────────────────────────────────────────────────────

# (name, dpi, max_px, grayscale, quality, split)
# Five render variants — first-match wins. The first strategy handles
# the vast majority of pages; the others are progressive fallbacks for
# difficult pages (lower DPI, grayscale, split).
STRATEGIES: list[tuple[str, int, int, bool, int, bool]] = [
    ("150dpi/1024px", 150, 1024, False, 85, False),
    ("100dpi/768px", 100, 768, False, 85, False),
    ("72dpi/512px", 72, 512, False, 80, False),
    ("150dpi/1024px/gray", 150, 1024, True, 85, False),
    ("100dpi/768px/gray", 100, 768, True, 80, False),
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


def _ocr_table(img_path: Path) -> str:
    """Re-OCR with table-specific prompt. Returns Markdown table text."""
    return _call_vision_backend(img_path, _TABLE_PROMPT).strip()


_OCR_PROMPT = (
    "Transcribe all text from this image exactly as it appears. "
    "Preserve line breaks, paragraph structure and section numbering. "
    "Do not describe the image or add commentary. "
    "Output only the verbatim text."
)

_TABLE_PROMPT = (
    "Extract all tables from this image as Markdown tables using "
    "| delimiters. Include headers. For any text that is not a table, "
    "transcribe it verbatim preserving line breaks. Do not describe the image."
)


def _call_backend(img_path: Path) -> str:
    """Send image to the OCR backend with the OCR prompt, return text."""
    return _call_vision_backend(img_path, _OCR_PROMPT)


def _call_vision_backend(img_path: Path, prompt: str) -> str:
    """POST an image + prompt to the vLLM chat-completions endpoint."""
    with open(img_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()

    url = settings.backend_url.rstrip("/")
    with httpx.Client(timeout=settings.backend_request_timeout_s) as client:
        r = client.post(
            f"{url}/v1/chat/completions",
            json={
                "model": settings.backend_model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                            },
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
                "max_tokens": 4096,
                "temperature": 0.0,
            },
        )
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"]


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
    """Try OCR, return (text, ok, elapsed_s).

    Exceptions are captured and logged (so a flaky backend or warmup 500s
    leave a debug trail) but not re-raised, because the outer strategy
    loop needs to move on to the next attempt.
    """
    try:
        t0 = time.time()
        text = _call_backend(img_path)
        elapsed = time.time() - t0
        if text.strip():
            return text.strip(), True, elapsed
        logger.warning("OCR returned empty text (%s, %.1fs)", label or img_path.name, elapsed)
        return "", False, 0.0
    except httpx.HTTPStatusError as e:
        logger.warning(
            "OCR HTTP %s for %s: %s",
            e.response.status_code,
            label or img_path.name,
            e.response.text[:200],
        )
        return "", False, 0.0
    except httpx.HTTPError as e:
        logger.warning("OCR transport error for %s: %s", label or img_path.name, e)
        return "", False, 0.0
    except Exception as e:
        logger.warning("OCR unexpected %s for %s: %s", type(e).__name__, label or img_path.name, e)
        return "", False, 0.0


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


MAX_STRATEGIES: int = len(STRATEGIES)  # try all configured strategies


def _detect_columns(img_path: Path) -> bool:
    """Detect multi-column layout via sliding-window search for a vertical gutter.

    Scans the center 40% of the page (30%-70% width) for a narrow vertical
    strip (~10px) that is significantly brighter than its 50px surroundings —
    indicating a column gutter between text columns.
    """
    try:
        import numpy as np

        img = Image.open(img_path).convert("L")
        w, h = img.size
        if w < 200 or h < 200:
            return False

        # Sample band from 25%-75% of page height (skip header/footer)
        band = img.crop((0, int(h * 0.25), w, int(h * 0.75)))
        col_means = np.array(band, dtype=np.float32).mean(axis=0)

        # Search in center 40% of width for a bright gap
        search_start = int(w * 0.30)
        search_end = int(w * 0.70)
        zone = col_means[search_start:search_end]

        best_diff = 0.0
        for x in range(25, len(zone) - 25):
            strip = zone[x - 5 : x + 5].mean()
            left_ctx = zone[max(0, x - 50) : x - 10].mean()
            right_ctx = zone[x + 10 : min(len(zone), x + 50)].mean()
            diff = strip - (left_ctx + right_ctx) / 2
            if diff > best_diff:
                best_diff = diff

        # Threshold 15: separates single-column (~3) from multi-column (~25+).
        # Deliberately generous — catching false positives is cheaper than
        # missing real multi-column pages (which would fail OCR entirely on
        # insurance documents with tight column gutters).
        return best_diff > 15
    except Exception:
        return False


def _try_column_split_ocr(src_image: Path, tmp_dir: Path, page_num: int) -> PageResult | None:
    """Try left/right column split OCR sequentially.

    Kept sequential because concurrent left+right + parallel page workers
    exceeded the backend's slots → queuing + timeouts → quality regressions.

    Returns PageResult on success, None on failure.
    """
    try:
        work_path = tmp_dir / f"pg{page_num}.jpg"
        img = Image.open(src_image).convert("RGB")
        img.save(work_path, "JPEG", quality=95)
        left_path, right_path = split_page_columns(work_path, tmp_dir, page_num)
        work_path.unlink(missing_ok=True)

        left_text, left_ok, _ = try_ocr(left_path)
        right_text, right_ok, _ = try_ocr(right_path)

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
    return None


def _ocr_image_with_strategies(src_image: Path, tmp_dir: Path, page_num: int) -> PageResult:
    """Run strategies against a *single* image input (no PDF extraction).

    If the image looks like a multi-column layout, tries column-split FIRST
    (shortcut). Otherwise runs at most ``MAX_STRATEGIES`` normal strategies,
    then falls back to column-split as last resort.
    """
    # Shortcut: detect multi-column layout and try column-split FIRST
    if _detect_columns(src_image):
        result = _try_column_split_ocr(src_image, tmp_dir, page_num)
        if result is not None:
            return result

    for name, _dpi, max_px, gray, quality, split in STRATEGIES[:MAX_STRATEGIES]:
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

    # Last resort: column-split (if not already tried via shortcut)
    result = _try_column_split_ocr(src_image, tmp_dir, page_num)
    if result is not None:
        return result

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


MAX_PARALLEL_PAGES: int = 12  # vLLM on H100 handles high concurrency cleanly

# If the first N pages all fail with zero successes, the backend is dead —
# abort the pipeline instead of burning the full page budget on 500s.
CIRCUIT_BREAKER_FAILED_THRESHOLD: int = 3


def _process_single_page(img_path: Path, page_num: int, tmp_dir: Path) -> PageResult:
    """Full per-page pipeline: OCR strategies + table detection."""
    page_result = _ocr_image_with_strategies(img_path, tmp_dir, page_num)

    if page_result.text and _detect_table_patterns(page_result.text):
        try:
            table_text = _ocr_table(img_path)
            if table_text.strip():
                page_result.text = table_text
                page_result.is_table = True
        except Exception:
            pass  # keep original text on table-OCR failure

    return page_result


def run_ocr(
    input_path: Path,
    tmp_dir: Path,
    output_path: Path | None = None,
    output_format: str = "md",
    pages_dir: Path | None = None,
    max_parallel: int = MAX_PARALLEL_PAGES,
) -> OcrResult:
    """Public entry point for the OCR pipeline (v5, parallelized).

    Processes up to ``max_parallel`` pages concurrently via a thread pool.
    Results are written incrementally (md/txt) in page order as contiguous
    completions become available. json/toon are written in full at the end.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

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

    # 3. Parallel OCR (bounded by max_parallel; backend serves concurrently)
    n = len(page_images)
    results: list[PageResult | None] = [None] * n
    next_to_write = 0

    with ThreadPoolExecutor(max_workers=max(1, max_parallel)) as executor:
        future_to_idx = {
            executor.submit(_process_single_page, img, i + 1, tmp_dir): i
            for i, img in enumerate(page_images)
        }

        failed_so_far = 0
        ok_so_far = 0
        for future in as_completed(future_to_idx):
            i = future_to_idx[future]
            try:
                results[i] = future.result()
            except Exception:
                results[i] = PageResult(number=i + 1, text=None, strategy="ERROR", elapsed_s=0.0)

            if results[i].text:
                ok_so_far += 1
            else:
                failed_so_far += 1

            # Circuit breaker: if the first several pages all fail with zero
            # successes, the backend is unreachable/broken. Abort before
            # firing more requests at it so the job surfaces a real error
            # instead of burning the full page budget.
            if (
                failed_so_far >= CIRCUIT_BREAKER_FAILED_THRESHOLD
                and ok_so_far == 0
            ):
                for f in future_to_idx:
                    f.cancel()
                raise RuntimeError(
                    f"OCR-Backend nicht erreichbar: erste {failed_so_far} Seiten "
                    "alle fehlgeschlagen, keine erfolgreich — Abbruch."
                )

            # Write out contiguous completed pages (preserves order)
            while next_to_write < n and results[next_to_write] is not None:
                # Merge boundary with previous page (if both present)
                if next_to_write > 0 and results[next_to_write - 1] is not None:
                    pair = [results[next_to_write - 1], results[next_to_write]]
                    _merge_across_boundaries(pair)
                if writer:
                    writer.append_chunk([results[next_to_write]])
                next_to_write += 1

    all_pages = [p for p in results if p is not None]

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
