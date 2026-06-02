"""Subprocess entry for the OCR pipeline.

Run via:
    python -m app.services.ocr_runner --input <path> --tmp-dir <path> \
        --output-json <path> [--output-path <path>] [--output-format md] \
        [--pages-dir <path>] [--engine auto|opendataloader|vllm]

Isolates backend and subprocess crashes from the ARQ worker: if the pipeline
(pdftoppm, Pillow, the OCR backend, or the opendataloader Java process)
segfaults, only this subprocess dies — the worker catches
``CalledProcessError`` / ``TimeoutExpired`` and continues serving jobs.

Engines:
    auto             — pick automatically (digital PDF → opendataloader,
                       else vllm). Default.
    opendataloader   — force CPU-only PDF parser. Exits with code 2 if
                       the result is too sparse to be acceptable (the
                       worker can then re-invoke with --engine vllm).
    vllm             — force the vision-LLM pipeline.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Exit codes that the worker interprets specially
EXIT_OK = 0
EXIT_OPENDATALOADER_UNACCEPTABLE = 2


def main() -> int:
    parser = argparse.ArgumentParser(description="DocklyOCR pipeline subprocess runner")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--tmp-dir", required=True, type=Path)
    parser.add_argument("--output-json", required=True, type=Path)
    parser.add_argument("--output-path", type=Path, default=None)
    parser.add_argument("--output-format", type=str, default="md")
    parser.add_argument("--pages-dir", type=Path, default=None)
    parser.add_argument(
        "--engine",
        choices=["auto", "opendataloader", "vllm"],
        default="auto",
    )
    parser.add_argument(
        "--structure-path",
        type=Path,
        default=None,
        help="Write the opendataloader JSON structure sidecar to this path (opendataloader only).",
    )
    parser.add_argument(
        "--html-path",
        type=Path,
        default=None,
        help="Write the opendataloader HTML preview sidecar to this path (opendataloader only).",
    )
    parser.add_argument(
        "--sanitize",
        action="store_true",
        help="When using opendataloader, replace emails/phones/IPs/etc. with placeholders.",
    )
    args = parser.parse_args()

    args.tmp_dir.mkdir(parents=True, exist_ok=True)
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    if args.output_path:
        args.output_path.parent.mkdir(parents=True, exist_ok=True)
    if args.pages_dir:
        args.pages_dir.mkdir(parents=True, exist_ok=True)

    # Imports are deferred so a job that ends up on opendataloader doesn't
    # pay the cost of importing the vision pipeline (and vice versa).
    if args.engine == "auto":
        from app.services.document_router import select_engine

        engine = select_engine(args.input)
    else:
        engine = args.engine

    if engine == "opendataloader":
        from app.services.document_router import is_result_acceptable
        from app.services.opendataloader_pipeline import run_opendataloader

        result = run_opendataloader(
            args.input,
            args.tmp_dir,
            output_path=args.output_path,
            pages_dir=args.pages_dir,
            structure_path=args.structure_path,
            html_path=args.html_path,
            sanitize=args.sanitize,
        )
        if not is_result_acceptable(result):
            # Signal the worker that a vllm fallback is needed. We do
            # NOT write the output files — the worker will re-invoke us
            # with --engine vllm.
            print(
                "opendataloader result too sparse — needs vllm fallback",
                file=sys.stderr,
            )
            return EXIT_OPENDATALOADER_UNACCEPTABLE
    else:  # vllm
        from app.services.ocr_pipeline import run_ocr

        result = run_ocr(
            args.input,
            args.tmp_dir,
            output_path=args.output_path,
            output_format=args.output_format,
            pages_dir=args.pages_dir,
        )

    args.output_json.write_text(json.dumps(result.to_json_dict(), ensure_ascii=False, indent=2))
    # Record the engine that produced this output for the worker to read.
    engine_marker = Path(f"{args.output_json}.engine")
    engine_marker.write_text(engine)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
