"""Normalization package exports."""

from backend.app.pipeline.normalization.merge import create_document_page_result, merge_page_blocks
from backend.app.pipeline.normalization.schema import DocumentPageResult, ExtractedBlock, ExtractionResult

__all__ = [
    "ExtractedBlock",
    "DocumentPageResult",
    "ExtractionResult",
    "merge_page_blocks",
    "create_document_page_result",
]
