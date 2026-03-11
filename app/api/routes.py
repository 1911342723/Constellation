"""Constellation API routes."""
from __future__ import annotations

import logging
from typing import List, Tuple

from fastapi import APIRouter, File, HTTPException, UploadFile

from app.api.schemas import (
    DocxParseResponse,
    FullParseResponse,
    HealthResponse,
    PaperParseResponse,
    ParseRequest,
    ParseResponse,
)
from app.core.config.settings import settings
from infrastructure.models import Block
from infrastructure.providers import DocxProvider, TextProvider
from modules.parser.parser import CaliperParser

logger = logging.getLogger(__name__)

router = APIRouter()

_shared_parser = CaliperParser()
_SUPPORTED_UPLOAD_FORMATS = {
    ".docx": "docx",
    ".txt": "txt",
}


def _ensure_supported_upload(filename: str | None) -> Tuple[str, str]:
    if not filename:
        raise HTTPException(status_code=400, detail="???????")

    lower_name = filename.lower()
    for suffix, source_format in _SUPPORTED_UPLOAD_FORMATS.items():
        if lower_name.endswith(suffix):
            return filename, source_format

    raise HTTPException(
        status_code=400,
        detail="??? .docx ? .txt ????? .doc ????",
    )


def _extract_blocks_from_upload(source_format: str, content: bytes) -> List[Block]:
    if source_format == "docx":
        return DocxProvider().extract_from_bytes(content)
    if source_format == "txt":
        return TextProvider().extract_from_bytes(content)
    raise HTTPException(status_code=400, detail=f"????????: {source_format}")


@router.get("/health", response_model=HealthResponse)
async def health_check():
    """Service liveness probe."""
    return HealthResponse(status="healthy", service=settings.app_name, version=settings.app_version)


@router.post("/parse", response_model=ParseResponse)
async def parse_blocks(request: ParseRequest):
    """Parse a Block list into a document tree (JSON + Markdown)."""
    blocks = [Block(**block_data) for block_data in request.blocks]

    document_tree = await _shared_parser.async_parse(blocks)

    return ParseResponse(
        success=True,
        document_tree=document_tree.to_dict(),
        markdown=document_tree.to_markdown(),
        json=document_tree.to_json(),
        sections=document_tree.to_markdown_sections(),
    )


@router.post("/parse/paper", response_model=PaperParseResponse)
async def parse_for_paper_editor(request: ParseRequest):
    """Parse Blocks into PaperData format for the typesetting system."""
    blocks = [Block(**block_data) for block_data in request.blocks]

    document_tree = await _shared_parser.async_parse(blocks)

    paper_data = document_tree.to_paper_data()
    if request.title:
        paper_data["title"] = request.title
    if request.authors:
        paper_data["authors"] = request.authors

    return PaperParseResponse(success=True, paper_data=paper_data)


@router.post("/parse/file", response_model=DocxParseResponse)
@router.post("/parse/docx", response_model=DocxParseResponse)
async def parse_uploaded_file(file: UploadFile = File(...)):
    """Extract blocks from a supported uploaded file (Stage 1 only)."""
    filename, source_format = _ensure_supported_upload(file.filename)

    content = await file.read()
    logger.info(
        "[Constellation] File received: %s (%d bytes, format=%s)",
        filename,
        len(content),
        source_format,
    )

    blocks = _extract_blocks_from_upload(source_format, content)
    logger.info("[Constellation] Stage 1 done: %d blocks", len(blocks))

    return DocxParseResponse(
        success=True,
        blocks=[block.model_dump() for block in blocks],
        filename=filename,
        source_format=source_format,
        total_blocks=len(blocks),
    )


@router.post("/parse/full/file", response_model=FullParseResponse)
@router.post("/parse/full", response_model=FullParseResponse)
async def parse_uploaded_file_full(file: UploadFile = File(...)):
    """Run the full pipeline for a supported uploaded file."""
    filename, source_format = _ensure_supported_upload(file.filename)

    content = await file.read()
    logger.info(
        "[Constellation] Full-pipeline request: %s (%d bytes, format=%s)",
        filename,
        len(content),
        source_format,
    )

    blocks = _extract_blocks_from_upload(source_format, content)
    logger.info("[Constellation] Stage 1 done: %d blocks", len(blocks))

    document_tree = await _shared_parser.async_parse(blocks)

    sections = document_tree.to_markdown_sections()
    full_markdown = document_tree.to_markdown()
    paper_data = document_tree.to_paper_data()
    stats = document_tree.get_stats()

    if not paper_data.get("title"):
        paper_data["title"] = filename.rsplit(".", 1)[0]

    logger.info(
        "[Constellation] Pipeline complete: '%s', %d sections",
        stats["doc_title"],
        stats["total_sections"],
    )

    return FullParseResponse(
        success=True,
        doc_title=document_tree.doc_title,
        doc_authors=document_tree.doc_authors,
        source_format=source_format,
        sections=sections,
        full_markdown=full_markdown,
        paper_data=paper_data,
        stats=stats,
    )
