"""Document renderers.

Every export format is produced from the same numbered document tree, so the
PDF, the Word file, the JSON payload and the on-screen preview always say the
same thing with the same clause numbers.
"""

from __future__ import annotations

from dataclasses import dataclass

from .docx_export import render_docx
from .html import build_document, render_html
from .json_export import render_json
from .markdown_export import render_markdown
from .pdf import PDF_AVAILABLE, pdf_unavailable_reason, render_pdf


@dataclass(frozen=True)
class ExportFormat:
    key: str
    label: str
    extension: str
    mimetype: str


FORMATS: dict[str, ExportFormat] = {
    "pdf": ExportFormat("pdf", "PDF", "pdf", "application/pdf"),
    "docx": ExportFormat(
        "docx",
        "Word (DOCX)",
        "docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
    "html": ExportFormat("html", "HTML", "html", "text/html; charset=utf-8"),
    "json": ExportFormat("json", "JSON", "json", "application/json"),
    "md": ExportFormat("md", "Markdown", "md", "text/markdown; charset=utf-8"),
}

__all__ = [
    "FORMATS",
    "PDF_AVAILABLE",
    "ExportFormat",
    "build_document",
    "pdf_unavailable_reason",
    "render_docx",
    "render_html",
    "render_json",
    "render_markdown",
    "render_pdf",
]
