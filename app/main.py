"""Cursor-Caliper FastAPI Application — document structure extraction service."""

import sys
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import router
from app.core.config.settings import settings
from app.core.exceptions import (
    ProviderError,
    CompressorError,
    LLMRouterError,
    AssemblerError,
    ParserError,
)

logger = logging.getLogger(__name__)

# ── Application factory ──────────────────────────────────────
app = FastAPI(
    title="Cursor-Caliper API",
    description="基于游标卡尺映射法的零损耗文档结构化提取服务",
    version=settings.app_version,
    docs_url="/docs",
    redoc_url="/redoc",
)

# ── CORS ─────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Global exception handlers ────────────────────────────────
# These replace the per-route try/except boilerplate.  Each domain
# exception maps to a semantically appropriate HTTP status code.


@app.exception_handler(ProviderError)
async def _handle_provider_error(request: Request, exc: ProviderError):
    """Document provider failures (bad file, unsupported format)."""
    logger.warning("[ProviderError] %s", exc.message)
    return JSONResponse(status_code=400, content={"success": False, "detail": exc.message})


@app.exception_handler(CompressorError)
async def _handle_compressor_error(request: Request, exc: CompressorError):
    """Skeleton compression failures (empty input, internal bug)."""
    logger.error("[CompressorError] %s", exc.message)
    return JSONResponse(status_code=500, content={"success": False, "detail": exc.message})


@app.exception_handler(LLMRouterError)
async def _handle_llm_error(request: Request, exc: LLMRouterError):
    """LLM call failures (timeout, bad response, quota exceeded)."""
    logger.error("[LLMRouterError] %s", exc.message)
    return JSONResponse(status_code=502, content={"success": False, "detail": exc.message})


@app.exception_handler(AssemblerError)
async def _handle_assembler_error(request: Request, exc: AssemblerError):
    """Interval resolver / tree assembly failures."""
    logger.error("[AssemblerError] %s", exc.message)
    return JSONResponse(status_code=500, content={"success": False, "detail": exc.message})


@app.exception_handler(ParserError)
async def _handle_parser_error(request: Request, exc: ParserError):
    """Top-level parser pipeline failures."""
    logger.error("[ParserError] %s", exc.message)
    return JSONResponse(status_code=500, content={"success": False, "detail": exc.message})


# ── Routes ───────────────────────────────────────────────────
app.include_router(router, prefix="/api/v1", tags=["parser"])


@app.get("/")
async def root():
    return {
        "service": "Cursor-Caliper",
        "version": settings.app_version,
        "description": "游标卡尺文档解析服务",
        "docs": "/docs",
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=28001, reload=True)
