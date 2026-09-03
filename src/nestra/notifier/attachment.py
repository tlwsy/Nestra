"""Attachment header, MIME sniffing, and streaming limit helpers."""

from __future__ import annotations

import fnmatch
import io
import re
import unicodedata
import zipfile
from collections.abc import Iterable
from email.message import Message
from email.utils import collapse_rfc2231_value
from urllib.parse import unquote


class AttachmentTooLarge(ValueError):
    pass


def sanitize_filename(filename: str, *, max_bytes: int = 255) -> str:
    """Make an untrusted display filename harmless; storage never uses this name."""
    name = re.sub(r"[\x00-\x1f\x7f/\\]+", "_", filename)
    name = "".join("_" if unicodedata.category(char).startswith("C") else char for char in name)
    name = unicodedata.normalize("NFC", name).strip(" .") or "attachment"
    raw = name.encode("utf-8")
    if len(raw) > max_bytes:
        name = raw[:max_bytes].decode("utf-8", "ignore").rstrip(" .") or "attachment"
    return name


def filename_from_content_disposition(header: str | None, fallback: str = "attachment") -> str:
    """Decode RFC 5987 ``filename*`` first, then legacy ``filename``."""
    if not header:
        return sanitize_filename(fallback)
    message = Message()
    message["Content-Disposition"] = header
    plain: str | None = None
    extended: str | None = None
    params = message.get_params(header="Content-Disposition", unquote=True) or []
    for key, value in params[1:]:
        if key.lower() != "filename":
            continue
        if isinstance(value, tuple):
            extended = collapse_rfc2231_value(value, errors="replace")
        elif plain is None:
            plain = unquote(value, encoding="utf-8", errors="replace")
    return sanitize_filename(extended or plain or fallback)


def sniff_mime(data: bytes) -> str:
    """Sniff common allowed attachment formats from bytes, never server headers."""
    signatures = (
        (b"%PDF-", "application/pdf"),
        (b"\x89PNG\r\n\x1a\n", "image/png"),
        (b"\xff\xd8\xff", "image/jpeg"),
        (b"GIF87a", "image/gif"),
        (b"GIF89a", "image/gif"),
        (b"Rar!\x1a\x07", "application/vnd.rar"),
        (b"7z\xbc\xaf'\x1c", "application/x-7z-compressed"),
        (b"\x1f\x8b", "application/gzip"),
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "application/x-ole-storage"),
    )
    for magic, mime in signatures:
        if data.startswith(magic):
            return mime
    if data.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = archive.namelist()
        except (OSError, zipfile.BadZipFile):
            return "application/octet-stream"
        office = {
            "word/": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "xl/": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "ppt/": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        }
        return next(
            (mime for prefix, mime in office.items() if any(n.startswith(prefix) for n in names)),
            "application/zip",
        )
    return "application/octet-stream"


def mime_allowed(mime: str, allow: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(mime.lower(), pattern.lower()) for pattern in allow)


def content_length_within_limit(value: str | None, max_bytes: int) -> bool:
    if not value:
        return True
    try:
        return 0 <= int(value) <= max_bytes
    except ValueError:
        return False


def read_limited(chunks: Iterable[bytes], max_bytes: int) -> bytes:
    """Consume a byte stream while enforcing the limit even if headers lied."""
    result = bytearray()
    for chunk in chunks:
        if len(result) + len(chunk) > max_bytes:
            raise AttachmentTooLarge(f"attachment exceeds {max_bytes} bytes")
        result.extend(chunk)
    return bytes(result)
