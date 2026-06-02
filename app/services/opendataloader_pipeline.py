"""OCR-free pipeline for digital PDFs using opendataloader-pdf.

Sits next to :func:`app.services.ocr_pipeline.run_ocr` and produces the
same :class:`OcrResult` shape so the worker / runner don't need engine
awareness beyond a single dispatch decision.

Used when the input is a digital PDF with a real text layer — much
faster than running a vision LLM, deterministic (no hallucinations),
and CPU-only (no GPU activation needed).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from app.services.ocr_pipeline import OcrResult, PageResult

logger = logging.getLogger(__name__)

# Sentinel string we ask opendataloader to insert between pages in the
# markdown output. Picked to be unique and HTML-safe so it survives any
# markdown rendering and is trivial to split on.
_PAGE_SEPARATOR = "\n\n<!--ODL-PAGE-BREAK-->\n\n"


def run_opendataloader(
    input_path: Path,
    tmp_dir: Path,
    output_path: Path | None = None,
    pages_dir: Path | None = None,  # noqa: ARG001 -- ODL writes its own pages dir, ignored
) -> OcrResult:
    """Run opendataloader-pdf against a digital PDF.

    Args:
        input_path: PDF to parse.
        tmp_dir: Scratch directory the wrapper writes its raw output to.
        output_path: Optional path where the final assembled markdown
            should be written (matches what ``run_ocr`` does).
        pages_dir: Unused here (kept for signature compatibility with
            ``run_ocr``). opendataloader does not need rendered page
            images; if set, it is left empty.

    Returns:
        ``OcrResult`` with one ``PageResult`` per source page.
    """
    from opendataloader_pdf import convert

    tmp_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    convert(
        str(input_path),
        output_dir=str(tmp_dir),
        format="markdown",
        markdown_page_separator=_PAGE_SEPARATOR,
        quiet=True,
        # external image references rather than base64-embeds → smaller MD
        image_output="off",
    )
    elapsed = time.time() - t0

    # opendataloader names the output ``<input_stem>.md`` in output_dir.
    md_file = tmp_dir / f"{input_path.stem}.md"
    if not md_file.exists():
        # Fallback for older versions / unexpected naming
        candidates = list(tmp_dir.glob("*.md"))
        if not candidates:
            raise RuntimeError(f"opendataloader produced no markdown output in {tmp_dir}")
        md_file = candidates[0]

    full_text = md_file.read_text(encoding="utf-8")

    # Split on our sentinel. opendataloader emits the separator BETWEEN
    # pages, so N pages → N-1 separators → N chunks.
    chunks = full_text.split(_PAGE_SEPARATOR)

    pages: list[PageResult] = []
    # Per-page elapsed: we only have a single total time. Distribute it
    # evenly so the admin UI shows non-zero numbers.
    per_page = elapsed / max(1, len(chunks))
    for i, chunk in enumerate(chunks, start=1):
        text = chunk.strip() or None
        pages.append(
            PageResult(
                number=i,
                text=text,
                strategy="opendataloader",
                elapsed_s=round(per_page, 3),
            )
        )

    pages_ok = sum(1 for p in pages if p.text)
    pages_failed = len(pages) - pages_ok

    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        # We strip our sentinel so the user-visible markdown is clean.
        output_path.write_text(full_text.replace(_PAGE_SEPARATOR, "\n\n"), encoding="utf-8")

    logger.info(
        "opendataloader %s: %d pages, %d ok, %d empty, %.2fs",
        input_path.name,
        len(pages),
        pages_ok,
        pages_failed,
        elapsed,
    )
    return OcrResult(
        pages=pages,
        page_count=len(pages),
        pages_ok=pages_ok,
        pages_failed=pages_failed,
    )
