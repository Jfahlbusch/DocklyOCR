"""HTTP header helpers shared across routers."""

from __future__ import annotations

import unicodedata
from urllib.parse import quote


def content_disposition_attachment(filename: str) -> str:
    """Build an RFC 6266-compliant ``Content-Disposition`` header value.

    HTTP header values are latin-1 only. A naive
    ``f'attachment; filename="{name}"'`` raises ``UnicodeEncodeError`` the
    moment the filename contains non-latin-1 characters — e.g. German
    umlauts, and especially macOS NFD filenames where ``ä`` is encoded as
    ``a`` + U+0308 (combining diaeresis).

    This emits both a sanitised ASCII ``filename=`` fallback (for ancient
    clients) and a percent-encoded UTF-8 ``filename*=`` per RFC 5987 (for
    every modern client), after normalising to NFC so macOS uploads render
    cleanly.
    """
    nfc = unicodedata.normalize("NFC", filename)
    ascii_fallback = nfc.encode("ascii", "replace").decode("ascii").replace('"', "")
    utf8_quoted = quote(nfc, safe="")
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{utf8_quoted}"
