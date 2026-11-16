"""Build the offline grid fixtures in ``test/fixtures/grids`` from the official PRISM zips.

    uv run python scripts/make_grid_fixtures.py /dir/with/official/zips

Hawaii and Puerto Rico are small and copied whole. The contiguous-US and Alaska grids
are cropped to a 1-degree window (Portland, Oregon) and a half-degree window
(Anchorage, Alaska) so the fixtures stay small but hold real values.
"""

from __future__ import annotations

import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "test" / "fixtures" / "grids"
WINDOWS = {"us": (45.0, 46.0, -123.5, -122.0), "ak": (60.9, 61.4, -150.2, -149.4)}


def header(text: str) -> dict[str, str]:
    """Key/value pairs of a BIL header."""
    return {p[0].upper(): p[1] for p in (line.split() for line in text.splitlines()) if len(p) >= 2}


def crop(src_zip: Path, code: str, window: tuple[float, float, float, float]) -> None:
    """Write a cropped copy of one region's grid zip."""
    stem = f"phzm_{code}_grid_2023"
    with zipfile.ZipFile(src_zip) as zf:
        h = header(zf.read(f"{stem}.hdr").decode())
        data = zf.read(f"{stem}.bil")
        terms = zf.read("phzm_terms_of_use.txt")
    ncols, dim = int(h["NCOLS"]), float(h["XDIM"])
    ulx, uly = float(h["ULXMAP"]), float(h["ULYMAP"])
    lat0, lat1, lon0, lon1 = window
    r0, r1 = round((uly - lat1) / dim), round((uly - lat0) / dim)
    c0, c1 = round((lon0 - ulx) / dim), round((lon1 - ulx) / dim)
    rows = [data[(r * ncols + c0) * 4 : (r * ncols + c1) * 4] for r in range(r0, r1)]
    new_h = dict(h)
    new_h.update(
        NROWS=str(r1 - r0),
        NCOLS=str(c1 - c0),
        BANDROWBYTES=str((c1 - c0) * 4),
        TOTALROWBYTES=str((c1 - c0) * 4),
        ULXMAP=f"{ulx + c0 * dim:.12f}",
        ULYMAP=f"{uly - r0 * dim:.12f}",
    )
    hdr_text = "".join(f"{k:<14} {v}\n" for k, v in new_h.items())
    with zipfile.ZipFile(OUT / f"{stem}.zip", "w", zipfile.ZIP_DEFLATED) as out:
        out.writestr(f"{stem}.hdr", hdr_text)
        out.writestr(f"{stem}.bil", b"".join(rows))
        out.writestr("phzm_terms_of_use.txt", terms)


def main() -> None:
    """Write all four fixtures."""
    src = Path(sys.argv[1])
    OUT.mkdir(parents=True, exist_ok=True)
    for code in ("hi", "pr"):
        shutil.copyfile(src / f"phzm_{code}_grid_2023.zip", OUT / f"phzm_{code}_grid_2023.zip")
    for code, window in WINDOWS.items():
        crop(src / f"phzm_{code}_grid_2023.zip", code, window)


if __name__ == "__main__":
    main()
