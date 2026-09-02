"""Materialise the long-PDF evaluation set from its provenance manifest.

The twelve evaluation documents are third-party publications (arXiv
preprints and one NIST publication).  They are referenced by source
rather than redistributed, so this script downloads them into
``tests/data/long_docs/`` and checks each file against the SHA-256 of
the copy the reported results were computed on.

    python -m evaluation.fetch_dataset            # download what is missing
    python -m evaluation.fetch_dataset --verify   # check only, never download
    python -m evaluation.fetch_dataset --force    # re-download everything

A checksum mismatch is not fatal.  arXiv re-renders and revises PDFs, so
a newer revision will differ from the reference copy.  What matters is
that ground-truth ``block_id`` values are coupled to the exact byte
stream: after a mismatch, re-run ``python -m evaluation.repair_gt`` to
realign the ground truth to the copy you actually downloaded.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MANIFEST = Path(__file__).resolve().parent / "datasets" / "long_docs_manifest.json"

# arXiv rejects the default urllib user agent.
_USER_AGENT = "Constellation-dataset-fetcher/1.0 (+https://github.com/1911342723/Constellation)"
_CHUNK = 1 << 16


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ssl_context(insecure: bool) -> ssl.SSLContext:
    if insecure:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def _download(url: str, target: Path, context: ssl.SSLContext) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    partial = target.with_suffix(target.suffix + ".part")
    with urllib.request.urlopen(request, timeout=120, context=context) as response:
        with partial.open("wb") as handle:
            while True:
                chunk = response.read(_CHUNK)
                if not chunk:
                    break
                handle.write(chunk)
    partial.replace(target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--verify", action="store_true", help="check existing files, download nothing")
    parser.add_argument("--force", action="store_true", help="re-download even if the file exists")
    parser.add_argument("--only", metavar="NAME", help="restrict to one file name or substring")
    parser.add_argument("--insecure", action="store_true",
                        help="skip TLS verification (integrity is still checked by SHA-256)")
    args = parser.parse_args(argv)

    context = _ssl_context(args.insecure)
    if args.insecure:
        print("TLS verification disabled; downloads are still verified by SHA-256.")

    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    target_dir = _REPO_ROOT / manifest["target_dir"]
    target_dir.mkdir(parents=True, exist_ok=True)

    documents = manifest["documents"]
    if args.only:
        documents = [d for d in documents if args.only in d["file"]]
        if not documents:
            print(f"no document in the manifest matches {args.only!r}", file=sys.stderr)
            return 2

    matched, mismatched, missing, failed = [], [], [], []

    for doc in documents:
        name = doc["file"]
        target = target_dir / name

        if args.force and not args.verify and target.exists():
            target.unlink()

        if not target.exists():
            if args.verify:
                missing.append(name)
                print(f"[missing ] {name}")
                continue
            print(f"[fetching] {name}  <- {doc['url']}")
            try:
                _download(doc["url"], target, context)
            except (urllib.error.URLError, OSError) as exc:
                failed.append((name, exc))
                print(f"[FAILED  ] {name}: {exc}")
                continue

        actual = _sha256(target)
        if actual == doc["sha256"]:
            matched.append(name)
            print(f"[ok      ] {name}")
        else:
            mismatched.append(name)
            print(f"[differs ] {name}")
            print(f"            expected {doc['sha256']}")
            print(f"            actual   {actual}")

    print()
    print(f"{len(matched)} match, {len(mismatched)} differ, {len(missing)} missing, {len(failed)} failed")

    if any("CERTIFICATE_VERIFY_FAILED" in str(exc) for _, exc in failed):
        print()
        print("TLS verification failed. This is usually a TLS-inspecting proxy whose root")
        print("certificate Python cannot see. Point SSL_CERT_FILE at your organisation's CA")
        print("bundle, or re-run with --insecure: either way every download is checked")
        print("against the SHA-256 recorded in the manifest.")

    if mismatched:
        print()
        print("Some documents differ from the reference copies. This is expected when the")
        print("publisher has issued a newer revision. Ground-truth block ids are tied to the")
        print("exact byte stream, so realign them before evaluating:")
        print()
        print("    python -m evaluation.repair_gt")
        print()
        print("Documents whose ground truth is `manual_expert_reference` (bert, resnet) must")
        print("instead be realigned with `python scripts/manual/realign_manual_gt.py`.")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
