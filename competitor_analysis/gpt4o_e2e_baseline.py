"""GPT-4o E2E baseline — pure LLM structural extraction.

Sends the full document text directly to GPT-4o and asks it to
produce a structured heading hierarchy end-to-end, without any
skeleton compression or physical-feature assistance.

This serves as the "pure LLM" upper-bound baseline, demonstrating
the cost and accuracy trade-offs vs Constellation's staged approach.

Usage:  python -m competitor_analysis.gpt4o_e2e_baseline
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


E2E_SYSTEM_PROMPT = """\
You are a document structure extractor. Given the full text of a document,
identify ALL section headings with their hierarchy levels.

Return a JSON object with:
- "doc_title": string
- "chapters": array of {"title": string, "level": integer (1-6)}

Rules:
- Level 1 = top-level chapter, Level 2 = section, Level 3 = subsection, etc.
- Include ALL headings, not just the first few.
- Do NOT include body text, only heading boundaries.
- Sort by document order.
"""


def extract_e2e(
    text: str,
    *,
    model: str = "gpt-4o",
    max_input_chars: int = 120000,
) -> dict:
    """Send document text to GPT-4o for end-to-end structure extraction.

    Returns dict with keys: chapters, tokens_used, time_s, truncated.
    """
    from openai import OpenAI
    from app.core.config import settings

    client = OpenAI(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url or None,
    )

    truncated = len(text) > max_input_chars
    if truncated:
        text = text[:max_input_chars]

    t0 = time.perf_counter()
    try:
        completion = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": E2E_SYSTEM_PROMPT},
                {"role": "user", "content": f"Extract headings from this document:\n\n{text}"},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
            max_tokens=4096,
        )

        content = completion.choices[0].message.content or "{}"
        parsed = json.loads(content)
        elapsed = time.perf_counter() - t0

        tokens_used = 0
        if completion.usage:
            tokens_used = completion.usage.total_tokens

        return {
            "chapters": parsed.get("chapters", []),
            "doc_title": parsed.get("doc_title", ""),
            "tokens_used": tokens_used,
            "time_s": elapsed,
            "truncated": truncated,
            "truncated_at": max_input_chars if truncated else len(text),
        }
    except Exception as e:
        return {
            "error": str(e),
            "chapters": [],
            "tokens_used": 0,
            "time_s": time.perf_counter() - t0,
            "truncated": truncated,
        }


def main():
    root_dir = Path(__file__).parent.parent
    data_dir = root_dir / "tests" / "data"

    if not data_dir.exists():
        print(f"Data dir not found: {data_dir}")
        return

    docx_files = sorted(data_dir.rglob("*.docx"))
    if not docx_files:
        print("No .docx files found")
        return

    print("=" * 70)
    print("  GPT-4o E2E Baseline Evaluation")
    print("=" * 70)

    for fpath in docx_files:
        print(f"\n--- {fpath.name} ---")

        from infrastructure.providers.docx_provider import DocxProvider
        provider = DocxProvider()
        blocks = provider.extract(str(fpath))
        full_text = "\n".join(b.text or "" for b in blocks)

        print(f"  Document chars: {len(full_text)}")
        result = extract_e2e(full_text)

        if "error" in result:
            print(f"  Error: {result['error']}")
        else:
            chapters = result["chapters"]
            print(f"  Headings found: {len(chapters)}")
            max_level = max((c.get("level", 1) for c in chapters), default=0)
            print(f"  Max heading depth: {max_level}")
            print(f"  Tokens used: {result['tokens_used']}")
            print(f"  Time: {result['time_s']:.2f}s")
            print(f"  Truncated: {result['truncated']}")
            if result["truncated"]:
                print(f"  Truncated at: {result['truncated_at']} chars")


if __name__ == "__main__":
    main()
