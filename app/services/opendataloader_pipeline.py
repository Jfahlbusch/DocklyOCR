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

# Element types in the opendataloader JSON tree that are worth anchoring
# (text-bearing blocks the user might want to click-jump to).
_ANCHORABLE_TYPES = frozenset({"heading", "paragraph", "list item"})


def _inject_bbox_anchors(markdown: str, structure_path: Path) -> str:
    """Prepend an invisible HTML anchor before each text block in ``markdown``
    that we can locate in the opendataloader JSON.

    Each anchor encodes ``page`` and ``bounding box`` so frontends can
    map an offset in the markdown back to a region in the PDF for
    highlighting:

        <a id="odl-p3-bbox-100-200-300-400"></a>
        ## § 6 Beratung des Versicherungsnehmers

    Block matching is content-based (first 40 chars normalised) — it's
    a best-effort decoration; if a block can't be matched the markdown
    is returned untouched at that position.
    """
    import json

    data = json.loads(structure_path.read_text(encoding="utf-8"))

    elements: list[dict] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            t = node.get("type")
            if t in _ANCHORABLE_TYPES and node.get("content") and node.get("bounding box"):
                elements.append(node)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)

    def _norm(s: str) -> str:
        return " ".join(s.split())[:40]

    # Build a lookup keyed by the first 40 characters of normalised content.
    by_head: dict[str, dict] = {}
    for el in elements:
        head = _norm(el["content"])
        by_head.setdefault(head, el)  # first occurrence wins on collision

    out_lines: list[str] = []
    for line in markdown.splitlines():
        stripped = line.lstrip("#-* ").strip()
        head = _norm(stripped) if stripped else ""
        el = by_head.pop(head, None) if head else None
        if el is not None:
            bbox = el["bounding box"]
            page = el.get("page number", 0)
            x1, y1, x2, y2 = (round(v, 1) for v in bbox)
            anchor = f'<a id="odl-p{page}-bbox-{x1}-{y1}-{x2}-{y2}"></a>'
            out_lines.append(anchor)
        out_lines.append(line)
    return "\n".join(out_lines)


def run_opendataloader(
    input_path: Path,
    tmp_dir: Path,
    output_path: Path | None = None,
    pages_dir: Path | None = None,  # noqa: ARG001 -- ODL writes its own pages dir, ignored
    structure_path: Path | None = None,
    sanitize: bool = False,
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
        structure_path: When set, the full JSON sidecar (one entry per
            element with bounding boxes, page numbers, heading levels,
            tables, fonts) is written here as well.
        sanitize: When True, opendataloader replaces e-mail addresses,
            phone numbers, IPs, credit card numbers and URLs with
            placeholders in the output. Useful for DSGVO-sensitive
            previews shared with third parties.

    Returns:
        ``OcrResult`` with one ``PageResult`` per source page.
    """
    from opendataloader_pdf import convert

    tmp_dir.mkdir(parents=True, exist_ok=True)
    # Always emit markdown; emit JSON too when we'll persist it as the
    # structure sidecar. JSON has a meaningful disk cost (~2-3x of MD on
    # AVB-style docs) so we only generate it when actually needed.
    fmt = "markdown,json" if structure_path is not None else "markdown"
    t0 = time.time()
    convert(
        str(input_path),
        output_dir=str(tmp_dir),
        format=fmt,
        markdown_page_separator=_PAGE_SEPARATOR,
        markdown_with_html=True,  # let us emit BBox anchors as <a id=...>
        quiet=True,
        sanitize=sanitize,
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

    # Persist the JSON sidecar to the caller-chosen path (worker → storage)
    if structure_path is not None:
        json_file = tmp_dir / f"{input_path.stem}.json"
        if not json_file.exists():
            # Fallback for unexpected naming
            json_candidates = list(tmp_dir.glob("*.json"))
            if json_candidates:
                json_file = json_candidates[0]
        if json_file.exists():
            structure_path.parent.mkdir(parents=True, exist_ok=True)
            structure_path.write_bytes(json_file.read_bytes())
        else:
            logger.warning(
                "opendataloader did not produce JSON in %s — skipping structure sidecar",
                tmp_dir,
            )

    # Inject bounding-box anchors into the markdown so frontends can map
    # text positions back to PDF coordinates (Phase 2 of the rich-output
    # work). Only attempt this when we already have the JSON in hand to
    # avoid a second parser pass.
    if structure_path is not None and structure_path.exists():
        try:
            full_text = _inject_bbox_anchors(full_text, structure_path)
        except Exception as e:  # never let annotation break the job
            logger.warning("bbox anchor injection failed (%s) — using plain markdown", e)

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
