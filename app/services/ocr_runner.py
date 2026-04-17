"""Subprocess entry for the OCR pipeline.

Run via:
    python -m app.services.ocr_runner --input <path> --tmp-dir <path> \
        --output-json <path> [--output-path <path>] [--output-format md] \
        [--pages-dir <path>]

Isolates backend and subprocess crashes from the ARQ worker: if the pipeline
(pdftoppm, Pillow, or an HTTP error from the OCR backend) segfaults, only
this subprocess dies — the worker catches ``CalledProcessError`` /
``TimeoutExpired`` and continues serving jobs.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.services.ocr_pipeline import run_ocr


def main() -> int:
    parser = argparse.ArgumentParser(description="DocklyOCR pipeline subprocess runner")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--tmp-dir", required=True, type=Path)
    parser.add_argument("--output-json", required=True, type=Path)
    parser.add_argument("--output-path", type=Path, default=None)
    parser.add_argument("--output-format", type=str, default="md")
    parser.add_argument("--pages-dir", type=Path, default=None)
    args = parser.parse_args()

    args.tmp_dir.mkdir(parents=True, exist_ok=True)
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    if args.output_path:
        args.output_path.parent.mkdir(parents=True, exist_ok=True)
    if args.pages_dir:
        args.pages_dir.mkdir(parents=True, exist_ok=True)

    result = run_ocr(
        args.input,
        args.tmp_dir,
        output_path=args.output_path,
        output_format=args.output_format,
        pages_dir=args.pages_dir,
    )
    args.output_json.write_text(json.dumps(result.to_json_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
