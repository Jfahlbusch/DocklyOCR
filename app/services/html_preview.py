"""Build a standalone HTML preview from a Markdown body.

Used for vllm-served (and vllm-fallback-after-opendataloader) jobs:
the engine itself produces a Markdown string, not HTML. We convert
that to a self-contained HTML document so the public ``/v1/jobs/{id}/preview``
endpoint works regardless of which engine produced the result.

opendataloader-served jobs sidestep this — they write their own HTML
sidecar directly from the PDF text layer, which preserves font-style
information that isn't reconstructable from Markdown.
"""

from __future__ import annotations

import html as _html

import markdown

# Minimal print-friendly styling — kept inline so the file is fully
# self-contained and renders identically when emailed, embedded in an
# iframe, or opened from disk.
_HTML_SHELL = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  :root {{ color-scheme: light; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    max-width: 800px;
    margin: 2rem auto;
    padding: 0 1rem;
    line-height: 1.55;
    color: #1f2937;
  }}
  h1, h2, h3, h4 {{ line-height: 1.25; margin-top: 1.6rem; }}
  h1 {{ font-size: 1.7rem; border-bottom: 1px solid #e5e7eb; padding-bottom: .3rem; }}
  h2 {{ font-size: 1.35rem; }}
  h3 {{ font-size: 1.15rem; }}
  table {{ border-collapse: collapse; margin: 1rem 0; }}
  th, td {{ border: 1px solid #d1d5db; padding: .35rem .6rem; }}
  th {{ background: #f3f4f6; text-align: left; }}
  code {{ background: #f3f4f6; padding: 1px 4px; border-radius: 3px; }}
  pre code {{ display: block; padding: .75rem; overflow-x: auto; }}
  hr {{ border: none; border-top: 1px solid #e5e7eb; margin: 1.5rem 0; }}
  .ocr-meta {{
    font-size: .8rem; color: #6b7280; margin-bottom: 1.5rem;
    padding: .5rem .75rem; background: #f9fafb; border-radius: 4px;
  }}
</style>
</head>
<body>
{meta_block}
{body}
</body>
</html>
"""


def build_preview_from_markdown(
    markdown_text: str,
    *,
    title: str = "OCR Preview",
    meta: dict[str, str] | None = None,
) -> str:
    """Convert ``markdown_text`` to a complete, self-contained HTML document.

    Args:
        markdown_text: The OCR result body in Markdown.
        title: Goes into ``<title>``; defaults to a generic label.
        meta: Optional key/value pairs rendered as a small grey meta-strip
            at the top of the body (e.g. engine, page counts, model).

    Returns:
        A complete HTML 5 document string ready to be served with
        ``Content-Type: text/html``.
    """
    body_html = markdown.markdown(
        markdown_text,
        extensions=["tables", "fenced_code", "sane_lists"],
        output_format="html",
    )

    meta_block = ""
    if meta:
        rows = " · ".join(
            f"<strong>{_html.escape(k)}:</strong> {_html.escape(v)}" for k, v in meta.items()
        )
        meta_block = f'<div class="ocr-meta">{rows}</div>'

    return _HTML_SHELL.format(
        title=_html.escape(title),
        meta_block=meta_block,
        body=body_html,
    )
