"""Fetch the arXiv PDFs behind the Comp-HRDoc test split.

Comp-HRDoc ships annotations only (MIT); the PDFs are fetched from arXiv by
identifier.  The annotation does not record a version, so every PDF is later
validated against the ground-truth page text (see ``prepare``) and documents
whose pages do not correspond are excluded and reported, never silently fixed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
import urllib.request
from pathlib import Path


def fetch(ids: list[str], out: Path, delay: float) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = out / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    for index, arxiv_id in enumerate(ids):
        target = out / f"{arxiv_id}.pdf"
        if target.exists() and arxiv_id in manifest:
            continue
        url = f"https://export.arxiv.org/pdf/{arxiv_id}"
        request = urllib.request.Request(url, headers={"User-Agent": "constellation-eval/1.0 (research benchmark)"})
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                data = response.read()
            if not data.startswith(b"%PDF"):
                raise ValueError("not a PDF")
            target.write_bytes(data)
            manifest[arxiv_id] = {"url": url, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
                                  "fetched": time.strftime("%Y-%m-%d")}
        except Exception as exc:  # recorded, not retried silently
            manifest[arxiv_id] = {"url": url, "error": type(exc).__name__}
        manifest_path.write_text(json.dumps(manifest, indent=1, sort_keys=True))
        print(f"[{index + 1}/{len(ids)}] {arxiv_id} {'ok' if 'sha256' in manifest[arxiv_id] else 'FAILED'}", flush=True)
        time.sleep(delay)
    return manifest


def main() -> None:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--toc-dir", type=Path, required=True)
    cli.add_argument("--out", type=Path, required=True)
    cli.add_argument("--delay", type=float, default=3.0)
    args = cli.parse_args()
    ids = sorted(path.stem for path in args.toc_dir.glob("*.json"))
    manifest = fetch(ids, args.out, args.delay)
    print(sum("sha256" in v for v in manifest.values()), "PDFs available")


if __name__ == "__main__":
    main()
