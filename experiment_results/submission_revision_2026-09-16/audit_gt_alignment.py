"""Read-only diagnostic: locate reference titles without consulting candidates.

Run from the repository root. This does not repair annotations or produce a
new accuracy estimate. A normalized exact match is only a review suggestion.
"""
import json
import re
import sys
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from infrastructure.providers.pdf_provider import PdfProvider


def norm(text):
    text = unicodedata.normalize("NFKC", text).casefold()
    text = re.sub(r"^\s*(?:\d+(?:\.\d+)*[.)]?\s+|[a-z]\.?\s+)", "", text)
    return "".join(char for char in text if char.isalnum())


def main():
    rows = []
    for name in ["attention_is_all_you_need", "bert", "chain_of_thought"]:
        print("audit", name, flush=True)
        blocks = PdfProvider().extract(str(ROOT / "tests/data/long_docs" / f"{name}.pdf"))
        by_id = {block.id: block for block in blocks}
        gt = json.loads((ROOT / "tests/data/long_docs/ground_truth" / f"{name}.json").read_text(encoding="utf-8"))
        for heading in gt["headings"]:
            if heading.get("block_id", -1) < 0:
                continue
            title = norm(heading["title"])
            block = by_id.get(heading["block_id"])
            old = norm(block.text or "") if block else ""
            exact = [item.id for item in blocks if item.type == "text" and norm(item.text or "") == title]
            rows.append({
                "document": name,
                "title": heading["title"],
                "stored_id": heading["block_id"],
                "stored_text": (block.text or "")[:180] if block else None,
                "stored_similarity": round(SequenceMatcher(None, title, old).ratio(), 3),
                "exact_title_ids": exact,
                "unique_exact_elsewhere": len(exact) == 1 and exact[0] != heading["block_id"],
            })
    Path(__file__).with_name("gt_alignment_audit.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({
        "audited": len(rows),
        "unique_exact_elsewhere": sum(row["unique_exact_elsewhere"] for row in rows),
        "stored_similarity_below_0_6": sum(row["stored_similarity"] < 0.6 for row in rows),
    }))


if __name__ == "__main__":
    main()
