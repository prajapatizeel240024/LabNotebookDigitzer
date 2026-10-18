"""Turning uploads into page images.

PDFs are never rendered all at once. The API only counts pages; each page job renders its own
page when a worker picks it up, so memory stays flat for a 300-page file and a very large
page is scaled down to max_image_px on its longest side before it goes to the model.
"""

from __future__ import annotations

import io
import uuid
from pathlib import Path

import pymupdf
from PIL import Image, ImageOps

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"
PDF_MAGIC = b"%PDF-"


class BadUpload(ValueError):
    pass


def sniff_kind(head: bytes) -> str:
    if head.startswith(PDF_MAGIC):
        return "pdf"
    if head.startswith(PNG_MAGIC) or head.startswith(JPEG_MAGIC):
        return "image"
    raise BadUpload("file must be a PDF, PNG or JPEG")


def pdf_page_count(path: Path) -> int:
    try:
        with pymupdf.open(path) as doc:
            if doc.needs_pass:
                raise BadUpload("PDF is password protected")
            n = doc.page_count
    except BadUpload:
        raise
    except Exception as exc:
        raise BadUpload(f"could not read PDF: {exc}") from exc
    if n < 1:
        raise BadUpload("PDF has no pages")
    return n


def normalize_image(raw: bytes, max_px: int) -> tuple[bytes, str]:
    """Return (bytes, mime). Bytes are left untouched unless the photo needs fixing.

    Phone photos often carry an EXIF rotation flag and can be 4000+ px wide. We apply the
    rotation and scale down to max_px, and re-encode only in those cases.
    """
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception as exc:
        raise BadUpload(f"could not read image: {exc}") from exc
    fmt = (img.format or "").upper()
    mime = "image/png" if fmt == "PNG" else "image/jpeg"
    rotated = img.getexif().get(0x0112, 1) not in (None, 1)
    too_big = max(img.size) > max_px
    if not rotated and not too_big:
        return raw, mime
    img = ImageOps.exif_transpose(img)
    if max(img.size) > max_px:
        img.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    if fmt == "PNG":
        img.save(out, format="PNG")
    else:
        img.convert("RGB").save(out, format="JPEG", quality=92)
    return out.getvalue(), mime


def render_pdf_page(path: Path, page_index: int, dpi: int, max_px: int) -> bytes:
    """Render one page (0-based) to PNG bytes, capped at max_px on the longest side."""
    with pymupdf.open(path) as doc:
        page = doc.load_page(page_index)
        zoom = dpi / 72.0
        longest = max(page.rect.width, page.rect.height) * zoom
        if longest > max_px:
            zoom *= max_px / longest
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        return pix.tobytes("png")


def write_atomic(path: Path, data: bytes) -> None:
    """Write via a temp file and rename, so a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
