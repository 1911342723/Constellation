import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from fastapi import HTTPException

from app.api.routes import _ensure_supported_upload, health_check
from app.core.config.settings import settings
from app.core.exceptions import ProviderError
from app.main import _build_cors_options
from infrastructure.providers.docx_provider import DocxProvider
from modules.parser.document_tree import DocumentTree
from modules.parser.parser import _LRUCache
from modules.parser.schemas import DocumentNode


def test_upload_guard_accepts_docx_and_txt():
    assert _ensure_supported_upload("report.DOCX") == ("report.DOCX", "docx")
    assert _ensure_supported_upload("notes.txt") == ("notes.txt", "txt")


@pytest.mark.parametrize("filename", [None, "legacy.doc", "slides.ppt"])
def test_upload_guard_rejects_unsupported_extensions(filename):
    with pytest.raises(HTTPException) as exc_info:
        _ensure_supported_upload(filename)

    assert exc_info.value.status_code == 400


def test_health_check_uses_runtime_settings():
    response = asyncio.run(health_check())

    assert response.service == settings.app_name
    assert response.version == settings.app_version


def test_cors_builder_disables_credentials_for_wildcard_origins():
    options = _build_cors_options(["*"], True)

    assert options["allow_origins"] == ["*"]
    assert options["allow_credentials"] is False


def test_provider_rejects_legacy_doc_paths_before_parsing():
    provider = DocxProvider()

    with pytest.raises(ProviderError) as exc_info:
        provider.extract("legacy.doc")

    assert ".docx" in exc_info.value.message


def test_lru_cache_returns_defensive_copies():
    cache = _LRUCache(max_size=1)
    original = DocumentTree(
        nodes=[
            DocumentNode(
                title="Intro",
                level=1,
                start_block_id=0,
                end_block_id=0,
                content="hello",
            )
        ],
        doc_title="Original",
    )

    cache.put("doc", original)
    original.doc_title = "Mutated after put"
    original.nodes[0].title = "Changed before get"

    cached = cache.get("doc")
    assert cached.doc_title == "Original"
    assert cached.nodes[0].title == "Intro"

    cached.doc_title = "Mutated after get"
    cached.nodes[0].title = "Changed after get"

    cached_again = cache.get("doc")
    assert cached_again.doc_title == "Original"
    assert cached_again.nodes[0].title == "Intro"
