"""Ingestion package exports."""

from backend.app.pipeline.ingestion.loader_docx import load_docx
from backend.app.pipeline.ingestion.loader_image import load_image
from backend.app.pipeline.ingestion.loader_pdf import load_pdf
from backend.app.pipeline.ingestion.loader_xlsx import load_xlsx

__all__ = ["load_pdf", "load_image", "load_docx", "load_xlsx"]
