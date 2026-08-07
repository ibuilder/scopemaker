"""PDF rendering with WeasyPrint.

The prototype this replaces rasterised the preview with html2canvas and dropped
a single PNG onto one A4 page, so anything longer than a page was scaled into
illegibility and no text was selectable or searchable.  WeasyPrint lays out real
paged media: text stays text, content flows across pages, and ``@page`` CSS
gives running headers, footers and "Page N of M".

WeasyPrint needs a native stack (Pango, cairo, GDK-PixBuf). It is present in
the Docker image and in CI; on a bare Windows box it is usually missing, so the
import is guarded and the failure is reported as an actionable message rather
than a stack trace.
"""

from __future__ import annotations

import logging
from typing import Any

from flask import current_app

from ...errors import RenderError
from ...models import Scope
from .html import render_html

logger = logging.getLogger(__name__)

PDF_AVAILABLE: bool
_IMPORT_ERROR: str | None

try:  # pragma: no cover - depends on the host's native libraries
    from weasyprint import HTML

    PDF_AVAILABLE = True
    _IMPORT_ERROR = None
except (ImportError, OSError) as exc:  # pragma: no cover
    HTML = None  # type: ignore[assignment]
    PDF_AVAILABLE = False
    _IMPORT_ERROR = str(exc)


INSTALL_HINT = (
    "PDF rendering requires WeasyPrint's native libraries (Pango, cairo, "
    "GDK-PixBuf).\n"
    "  Docker/Linux: they are installed in the provided image; "
    "on Debian/Ubuntu run\n"
    "    apt-get install -y libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b "
    "libffi-dev libjpeg-dev\n"
    "  macOS:   brew install pango libffi\n"
    "  Windows: install the GTK3 runtime, then restart the shell -- see "
    "https://doc.courtbouillon.org/weasyprint/stable/first_steps.html#windows\n"
    "Every other export format (DOCX, HTML, Markdown, JSON) works without it."
)


def pdf_unavailable_reason() -> str | None:
    """Why PDF export is disabled, or ``None`` when it works."""
    if PDF_AVAILABLE:
        return None
    return f"{_IMPORT_ERROR}\n\n{INSTALL_HINT}"


def render_pdf(scope: Scope, *, organization: Any = None) -> bytes:
    """Render a scope to a paginated, text-selectable PDF."""
    if not PDF_AVAILABLE:
        raise RenderError(
            "PDF export is not available on this server because WeasyPrint's "
            "native libraries are not installed.",
            code="pdf_unavailable",
            details={"hint": INSTALL_HINT, "import_error": _IMPORT_ERROR},
        )

    html = render_html(scope, organization=organization, standalone=True)

    # base_url lets WeasyPrint resolve the app's own stylesheets and any logo
    # referenced with a relative path.
    base_url = current_app.root_path

    try:
        document = HTML(string=html, base_url=base_url).render()
        return document.write_pdf()
    except Exception as exc:
        logger.exception("WeasyPrint failed to render scope %s", scope.id)
        raise RenderError(
            f"The PDF could not be generated: {exc}", code="pdf_render_failed"
        ) from exc


def render_pdf_pages(scope: Scope, *, organization: Any = None) -> int:
    """Page count without keeping the bytes -- used by tests and diagnostics."""
    if not PDF_AVAILABLE:
        raise RenderError("PDF export is not available.", code="pdf_unavailable")
    html = render_html(scope, organization=organization, standalone=True)
    document = HTML(string=html, base_url=current_app.root_path).render()
    return len(document.pages)
