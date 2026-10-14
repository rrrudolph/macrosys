"""Download an OECD ICIO year-block zip and record its sha256 (spec §9.6).

This module performs a network call. Tests must not import-and-call ``download_icio``;
the suite has no network and parses fixtures under ``tests/fixtures/io_tables/``.
No API key and no environment variable are required — the files are public HTTPS.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from macrosys.io_tables.layout import ICIO_2025, IcioLayout


def download_icio(year: int, dest: str | Path, layout: IcioLayout | None = None) -> Path:
    """Fetch the OECD zip that contains ``year`` and write a sha256 manifest beside it (spec §9.6).

    Network call: HTTP GET of the edition's zip URL (``layout.zip_url``), streamed to disk.
    No API key. ``dest`` is a directory, or a path ending in ``.zip``. The manifest is
    ``<zipname>.sha256.json`` next to the zip and records the URL, edition, year, byte
    count, and sha256. The zip holds one CSV per year; pass that CSV to ``parse_icio_csv``.
    Not for use in tests.
    """
    edition = ICIO_2025 if layout is None else layout
    url = edition.zip_url(int(year))
    filename = url.rsplit("/", 1)[-1]
    target = _target_path(Path(dest), filename)
    partial = target.with_suffix(target.suffix + ".partial")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        digest, nbytes = _http_get(url, partial)
        partial.replace(target)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    manifest = {
        "edition": edition.edition,
        "vintage": edition.vintage,
        "year": int(year),
        "url": url,
        "filename": target.name,
        "sha256": digest,
        "bytes": nbytes,
        "unit": edition.unit,
    }
    manifest_path = target.with_name(target.name + ".sha256.json")
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return target


def _target_path(dest: Path, filename: str) -> Path:
    if dest.suffix.lower() == ".zip":
        return dest
    return dest / filename


def _http_get(url: str, dest: Path) -> tuple[str, int]:
    """Network call. Stream the response to ``dest`` and return ``(sha256, nbytes)``."""
    import httpx

    digest = hashlib.sha256()
    nbytes = 0
    timeout = httpx.Timeout(30.0, read=600.0)
    headers = {"User-Agent": "macrosys/0.0.1 (ICIO research loader; +https://oe.cd/icio)"}
    with httpx.stream("GET", url, follow_redirects=True, timeout=timeout, headers=headers) as response:
        response.raise_for_status()
        with dest.open("wb") as handle:
            for chunk in response.iter_bytes(1024 * 1024):
                handle.write(chunk)
                digest.update(chunk)
                nbytes += len(chunk)
    return digest.hexdigest(), nbytes


def main(argv: list[str] | None = None) -> None:
    """CLI entry: ``python -m macrosys.io_tables.download YEAR DEST``."""
    parser = argparse.ArgumentParser(
        prog="python -m macrosys.io_tables.download",
        description="Download an OECD ICIO 2025 zip and write a sha256 manifest. Performs a network call.",
    )
    parser.add_argument("year", type=int, help="Table year, 1995–2022. The zip may also contain neighbouring years.")
    parser.add_argument("dest", type=Path, help="Directory for the zip, or a .zip path.")
    args = parser.parse_args(argv)
    path = download_icio(args.year, args.dest)
    text = path.with_name(path.name + ".sha256.json").read_text(encoding="utf-8")
    digest = json.loads(text)["sha256"]
    print(f"{digest}  {path}")


if __name__ == "__main__":
    main()
