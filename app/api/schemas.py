"""Pydantic schemas for the Constellation API."""
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class BlockSchema(BaseModel):
    """Serialized block payload."""

    id: int = Field(..., description="Block ID")
    type: str = Field(..., description="Block type: text, image, table, formula")
    text: Optional[str] = Field(None, description="Text content")
    image_data: Optional[str] = Field(None, description="Image payload (Base64 or URL)")
    caption: Optional[str] = Field(None, description="Image or table caption")
    table_data: Optional[Dict[str, Any]] = Field(None, description="Raw table data")
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Extra metadata")
    is_bold: bool = Field(default=False, description="Whether the block is bold")
    font_size: Optional[float] = Field(None, description="Font size in pt")
    alignment: Optional[str] = Field(None, description="Paragraph alignment")
    is_heading_style: bool = Field(default=False, description="Whether a heading style is applied")
    heading_level: Optional[int] = Field(None, description="Heading level")


class ParseRequest(BaseModel):
    """Request body for block-based parsing."""

    blocks: List[Dict[str, Any]] = Field(..., description="Input block list")
    title: Optional[str] = Field(None, description="Optional title override")
    authors: Optional[str] = Field(None, description="Optional authors override")


class SectionOutput(BaseModel):
    """Markdown section output."""

    title: str = Field(..., description="Section title")
    content: str = Field(..., description="Markdown content")
    section_type: str = Field(default="section", description="Section semantic type")
    level: int = Field(default=1, description="Section depth")


class ParseResponse(BaseModel):
    """Generic parsing response."""

    success: bool = Field(..., description="Whether parsing succeeded")
    document_tree: List[Dict[str, Any]] = Field(..., description="Document tree")
    markdown: str = Field(..., description="Full Markdown output")
    json_output: str = Field(..., description="JSON output", alias="json")
    sections: List[Dict[str, Any]] = Field(default_factory=list, description="Markdown sections")


class PaperParseResponse(BaseModel):
    """Paper-editor oriented response."""

    success: bool = Field(..., description="Whether parsing succeeded")
    paper_data: Dict[str, Any] = Field(..., description="PaperData payload")


class DocxParseResponse(BaseModel):
    """Stage-1 file extraction response."""

    success: bool = Field(..., description="Whether extraction succeeded")
    blocks: List[Dict[str, Any]] = Field(..., description="Extracted blocks")
    filename: str = Field(..., description="Original filename")
    source_format: str = Field(..., description="Source format: docx|txt")
    total_blocks: int = Field(..., description="Total block count")


class FullParseResponse(BaseModel):
    """Full-pipeline parsing response."""

    success: bool = Field(..., description="Whether parsing succeeded")
    doc_title: str = Field(default="", description="Detected document title")
    doc_authors: str = Field(default="", description="Detected authors")
    source_format: str = Field(default="docx", description="Source format: docx|txt")
    sections: List[Dict[str, Any]] = Field(..., description="Markdown sections")
    full_markdown: str = Field(..., description="Full Markdown output")
    paper_data: Dict[str, Any] = Field(..., description="PaperData payload")
    stats: Dict[str, Any] = Field(..., description="Parse statistics")


class HealthResponse(BaseModel):
    """Health-check response."""

    status: str = Field(..., description="Service status")
    service: str = Field(..., description="Service name")
    version: str = Field(..., description="Service version")
