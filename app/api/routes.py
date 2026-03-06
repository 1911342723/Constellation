"""Cursor-Caliper API Routes.

All domain exceptions (ProviderError, LLMRouterError, ParserError, etc.)
are caught by the global exception handlers registered in ``app.main``.
Route functions therefore contain **zero** try/except boilerplate — they
simply call the business logic and let exceptions propagate naturally.
"""
from __future__ import annotations

import logging
from typing import List

from fastapi import APIRouter, HTTPException, UploadFile, File

from infrastructure.models import Block
from infrastructure.providers import DocxProvider
from modules.parser.parser import CaliperParser
from app.api.schemas import (
    ParseRequest,
    ParseResponse,
    PaperParseResponse,
    HealthResponse,
    DocxParseResponse,
    FullParseResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_shared_parser = CaliperParser()


# ── Health ───────────────────────────────────────────────────

@router.get("/health", response_model=HealthResponse)
async def health_check():
    """Service liveness probe."""
    return HealthResponse(status="healthy", service="Cursor-Caliper", version="0.2.0")


# ── Block-level parsing ──────────────────────────────────────

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


# ── DOCX upload endpoints ────────────────────────────────────

@router.post("/parse/docx", response_model=DocxParseResponse)
async def parse_docx_file(file: UploadFile = File(...)):
    """Extract Blocks from a .docx file (Stage 1 only)."""
    if not file.filename.endswith((".docx", ".doc")):
        raise HTTPException(status_code=400, detail="只支持 .docx 或 .doc 文件")

    content = await file.read()
    logger.info("[Cursor-Caliper] File received: %s (%d bytes)", file.filename, len(content))

    provider = DocxProvider()
    blocks = provider.extract_from_bytes(content)
    logger.info("[Cursor-Caliper] Stage 1 done: %d blocks", len(blocks))

    return DocxParseResponse(
        success=True,
        blocks=[block.model_dump() for block in blocks],
        filename=file.filename,
        total_blocks=len(blocks),
    )


@router.post("/parse/full", response_model=FullParseResponse)
async def parse_docx_full(file: UploadFile = File(...)):
    """One-shot full-pipeline endpoint: .docx → structured Markdown.

    Runs all four Cursor-Caliper stages in sequence:
    1. DocxProvider  — physical block extraction
    2. SkeletonCompressor — virtual-space compression
    3. LLMRouter — AI cursor routing
    4. IntervalResolver — forced-closure assembly
    """
    if not file.filename.endswith((".docx", ".doc")):
        raise HTTPException(status_code=400, detail="只支持 .docx 或 .doc 文件")

    content = await file.read()
    logger.info("[Cursor-Caliper] Full-pipeline request: %s (%d bytes)", file.filename, len(content))

    # Stage 1
    provider = DocxProvider()
    blocks = provider.extract_from_bytes(content)
    logger.info("[Cursor-Caliper] Stage 1 done: %d blocks", len(blocks))

    # Stages 2-4 (async to avoid blocking the event loop)
    document_tree = await _shared_parser.async_parse(blocks)

    sections = document_tree.to_markdown_sections()
    full_markdown = document_tree.to_markdown()
    paper_data = document_tree.to_paper_data()
    stats = document_tree.get_stats()

    if not paper_data.get("title"):
        paper_data["title"] = file.filename.rsplit(".", 1)[0]

    logger.info(
        "[Cursor-Caliper] Pipeline complete: '%s', %d sections",
        stats["doc_title"],
        stats["total_sections"],
    )

    return FullParseResponse(
        success=True,
        doc_title=document_tree.doc_title,
        doc_authors=document_tree.doc_authors,
        sections=sections,
        full_markdown=full_markdown,
        paper_data=paper_data,
        stats=stats,
    )
