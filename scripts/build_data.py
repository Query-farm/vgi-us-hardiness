"""Build the bundled data files in ``src/us_hardiness_worker/data/``.

Run from the repository root:

    uv run python scripts/build_data.py            # fetch what is missing, then build
    uv run python scripts/build_data.py --offline  # build from files already in .cache/raw

Raw downloads go to ``.cache/raw`` (gitignored). The script is polite: a generic
User-Agent, one request per second, and every raw file is reused once fetched.

Sources (see DATA-SOURCES.md for licences and checksums):

* 2023 USDA Plant Hardiness Zone Map ZIP-code listings, PRISM Group, Oregon State
  University: https://prism.oregonstate.edu/phzm/
* USDA NRCS PLANTS Database: the complete checklist (``plantlst.txt``) and the
  characteristics search service used by https://plants.usda.gov/characteristics-search
  (``characteristicSearchResults`` lists the ~2,200 accepted taxa that have
  characteristics; ``characteristicSearchFilterResults?type=...`` gives, per
  characteristic value, the ids of the plants that have it). Numeric
  characteristics come from that service as ranges (``-47 - -43``), not exact values.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
import time
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / ".cache" / "raw"
OUT = ROOT / "src" / "us_hardiness_worker" / "data"
USER_AGENT = "vgi-us-hardiness/0.1 (+https://query.farm)"

PHZM_BASE = "https://prism.oregonstate.edu/phzm/data/2023/"
PHZM_REGIONS = {"us": "conus", "ak": "alaska", "hi": "hawaii", "pr": "puerto_rico"}
PLANTS_LIST_URL = "https://plants.sc.egov.usda.gov/DocumentLibrary/Txt/plantlst.txt"
PLANTS_API = "https://plantsservices.sc.egov.usda.gov/api/"
FILTER_TYPES = ["Filtering", "Growth Requirements", "Morphology", "Reproduction", "Suitability Use"]

STATE_CODES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA",
    "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE", "District of Columbia": "DC",
    "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL",
    "Indiana": "IN", "Iowa": "IA", "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA",
    "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN",
    "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR",
    "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD",
    "Tennessee": "TN", "Texas": "TX", "Utah": "UT", "Vermont": "VT", "Virginia": "VA",
    "Washington": "WA", "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
    "Guam": "GU", "Palau": "PW", "Puerto Rico": "PR", "U.S. Minor Outlying Islands": "UM",
    "Virgin Islands": "VI", "Navassa Island": "UM",
}  # fmt: skip

_last_request = 0.0


def fetch(url: str, dest: Path, *, offline: bool) -> Path:
    """Download ``url`` to ``dest`` unless it is already there."""
    global _last_request
    if dest.exists():
        return dest
    if offline:
        sys.exit(f"missing {dest} (run without --offline to fetch {url})")
    wait = 1.0 - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)
    print(f"fetch {url}", file=sys.stderr)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=180) as resp:
        body = resp.read()
    _last_request = time.monotonic()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)
    return dest


def sha256(path: Path) -> str:
    """Return the hex SHA-256 of a file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def strip_html(text: str | None) -> str | None:
    """Drop the ``<i>`` markup PLANTS puts in scientific names."""
    if text is None:
        return None
    return re.sub(r"<[^>]+>", "", text).strip() or None


def parse_range(label: str) -> tuple[float | None, float | None]:
    """Parse a PLANTS range label such as ``-47 - -43`` or ``5.1-5.2`` into its bounds."""
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*-\s*(-?\d+(?:\.\d+)?)\s*", label)
    if not m:
        return None, None
    return float(m.group(1)), float(m.group(2))


def half_zone_index(temp_f: float) -> int | None:
    """Index 0..25 of the half zone (1a..13b) containing ``temp_f``; None outside -60..70 °F."""
    if temp_f < -60 or temp_f >= 70:
        return None
    return int((temp_f + 60) // 5)


def half_zone_name(index: int) -> str:
    """``0`` -> ``1a``, ``25`` -> ``13b``."""
    return f"{index // 2 + 1}{'ab'[index % 2]}"


def build_zip_zones(offline: bool) -> tuple[pa.Table, dict[str, str]]:
    """Combine the four PHZM ZIP listings into one table."""
    cols: dict[str, list[Any]] = {k: [] for k in ("zip", "region", "zone", "half_zone", "low", "high")}
    sums = {}
    for code, region in PHZM_REGIONS.items():
        name = f"phzm_{code}_zipcode_2023.csv"
        path = fetch(PHZM_BASE + name, RAW / name, offline=offline)
        sums[name] = sha256(path)
        for row in csv.DictReader(io.StringIO(path.read_text(encoding="utf-8"))):
            half = row["zone"].strip()
            low_s, high_s = (s.strip() for s in row["trange"].split(" to "))
            low, high = int(low_s), int(high_s)
            idx = half_zone_index(low)
            if idx is None or half_zone_name(idx) != half or high != low + 5:
                raise ValueError(f"{name}: zone {half!r} does not match range {row['trange']!r}")
            cols["zip"].append(row["zipcode"].strip().zfill(5))
            cols["region"].append(region)
            cols["zone"].append(int(half[:-1]))
            cols["half_zone"].append(half)
            cols["low"].append(low)
            cols["high"].append(high)
    if len(set(cols["zip"])) != len(cols["zip"]):
        raise ValueError("duplicate ZIP codes across the PHZM listings")
    table = pa.table(
        {
            "zip": pa.array(cols["zip"], pa.string()),
            "region": pa.array(cols["region"], pa.string()),
            "zone": pa.array(cols["zone"], pa.int8()),
            "half_zone": pa.array(cols["half_zone"], pa.string()),
            "min_temp_f_low": pa.array(cols["low"], pa.int16()),
            "min_temp_f_high": pa.array(cols["high"], pa.int16()),
        }
    ).sort_by("zip")
    return table, sums


def build_plants(offline: bool) -> tuple[pa.Table, pa.Table, dict[str, Any]]:
    """Join the PLANTS checklist with the characteristics index."""
    lst = fetch(PLANTS_LIST_URL, RAW / "plantlst.txt", offline=offline)
    csr = fetch(PLANTS_API + "characteristicSearchResults", RAW / "characteristicSearchResults.json", offline=offline)
    filters: dict[str, list[dict[str, Any]]] = {}
    for t in FILTER_TYPES:
        dest = RAW / f"characteristicSearchFilterResults_{t.replace(' ', '_')}.json"
        url = PLANTS_API + "characteristicSearchFilterResults?type=" + urllib.request.quote(t)
        filters[t] = json.loads(fetch(url, dest, offline=offline).read_text(encoding="utf-8"))

    # Checklist: one row per symbol (accepted names and synonyms).
    rows: dict[str, dict[str, Any]] = {}
    reader = csv.DictReader(io.StringIO(lst.read_text(encoding="utf-8-sig")))
    for r in reader:
        accepted = r["Symbol"].strip()
        syn = r["Synonym Symbol"].strip()
        symbol = syn or accepted
        if symbol in rows:
            # A few synonym symbols are listed under more than one accepted name; keep the first.
            continue
        rows[symbol] = {
            "symbol": symbol,
            "accepted_symbol": accepted,
            "is_accepted": not syn,
            "scientific_name": r["Scientific Name with Author"].strip() or None,
            "common_name": r["Common Name"].strip() or None,
            "family": r["Family"].strip() or None,
        }
    # Synonyms carry no family in the checklist; inherit the accepted name's.
    for row in rows.values():
        if row["family"] is None and not row["is_accepted"]:
            acc = rows.get(row["accepted_symbol"])
            row["family"] = acc["family"] if acc else None

    # Characteristics: id -> symbol, then invert value -> plantIds.
    id_to_symbol: dict[int, str] = {}
    name_plain: dict[str, str | None] = {}
    for p in json.loads(csr.read_text(encoding="utf-8")):
        id_to_symbol[int(p["id"])] = p["symbol"]
        name_plain[p["symbol"]] = p.get("scientificNameWithoutAuthor")
    per_plant: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    long_rows: list[tuple[str, str, str, str]] = []
    unmatched_ids: set[int] = set()
    for ftype, options in filters.items():
        for opt in options:
            cname = opt["name"].replace("CaCO~3~", "CaCO3")
            category = opt.get("type") or ftype
            for f in opt["filters"]:
                value = re.sub(r"\s+", " ", f["display"]).strip()
                for pid in dict.fromkeys(f["plantIds"]):
                    sym = id_to_symbol.get(int(pid))
                    if sym is None:
                        unmatched_ids.add(int(pid))
                        continue
                    if value not in per_plant[sym][cname]:
                        per_plant[sym][cname].append(value)
                        if ftype != "Filtering":
                            long_rows.append((sym, category, cname, value))

    def one(sym: str, name: str) -> str | None:
        vals = [v for v in per_plant.get(sym, {}).get(name, []) if v != "N/A"]
        return ", ".join(sorted(vals)) if vals else None

    def rng(sym: str, name: str) -> tuple[float | None, float | None]:
        vals = [parse_range(v) for v in per_plant.get(sym, {}).get(name, []) if v != "N/A"]
        vals = [v for v in vals if v[0] is not None]
        if not vals:
            return None, None
        return min(v[0] for v in vals if v[0] is not None), max(v[1] for v in vals if v[1] is not None)

    out: dict[str, list[Any]] = defaultdict(list)
    for sym in sorted(rows):
        row = rows[sym]
        ch = per_plant.get(sym)
        for k, v in row.items():
            out[k].append(v)
        out["scientific_name_plain"].append(name_plain.get(sym))
        out["has_characteristics"].append(ch is not None)
        out["plant_group"].append(one(sym, "Group") if ch else None)
        out["growth_habit"].append(sorted(ch["Growth Habit"]) if ch and ch.get("Growth Habit") else None)
        out["duration"].append(sorted(ch["Duration"]) if ch and ch.get("Duration") else None)
        natives = None
        if ch and ch.get("Nativity Status"):
            natives = []
            for label in ch["Nativity Status"]:
                status, _, region = (s.strip() for s in label.rpartition(" - "))
                natives.append({"region": region, "status": status})
            natives.sort(key=lambda d: (d["region"], d["status"]))
        out["native_status"].append(natives)
        states = None
        if ch and ch.get("State/Province"):
            states = sorted({STATE_CODES[s] for s in ch["State/Province"] if s in STATE_CODES})
        out["states"].append(states or None)
        lo, hi = rng(sym, "Temperature, Minimum (°F)") if ch else (None, None)
        out["min_temp_f_low"].append(None if lo is None else int(lo))
        out["min_temp_f_high"].append(None if hi is None else int(hi))
        # DERIVED: the warm end of the PLANTS range is the coldest temperature the plant is
        # documented to tolerate for certain; clamp hardier plants to zone 1a.
        idx = None if hi is None else half_zone_index(max(hi, -60.0))
        out["plant_min_zone_derived"].append(None if idx is None else half_zone_name(idx))
        out["shade_tolerance"].append(one(sym, "Shade Tolerance") if ch else None)
        out["drought_tolerance"].append(one(sym, "Drought Tolerance") if ch else None)
        h_lo, h_hi = rng(sym, "Height at Maturity (feet)") if ch else (None, None)
        out["mature_height_ft_low"].append(h_lo)
        out["mature_height_ft_high"].append(h_hi)
        out["bloom_period"].append(one(sym, "Bloom Period") if ch else None)
        out["growth_rate"].append(one(sym, "Growth Rate") if ch else None)

    native_type = pa.list_(pa.struct([("region", pa.string()), ("status", pa.string())]))
    plants = pa.table(
        {
            "symbol": pa.array(out["symbol"], pa.string()),
            "accepted_symbol": pa.array(out["accepted_symbol"], pa.string()),
            "is_accepted": pa.array(out["is_accepted"], pa.bool_()),
            "scientific_name": pa.array(out["scientific_name"], pa.string()),
            "scientific_name_plain": pa.array(out["scientific_name_plain"], pa.string()),
            "common_name": pa.array(out["common_name"], pa.string()),
            "family": pa.array(out["family"], pa.string()),
            "has_characteristics": pa.array(out["has_characteristics"], pa.bool_()),
            "plant_group": pa.array(out["plant_group"], pa.string()),
            "growth_habit": pa.array(out["growth_habit"], pa.list_(pa.string())),
            "duration": pa.array(out["duration"], pa.list_(pa.string())),
            "native_status": pa.array(out["native_status"], native_type),
            "states": pa.array(out["states"], pa.list_(pa.string())),
            "min_temp_f_low": pa.array(out["min_temp_f_low"], pa.int16()),
            "min_temp_f_high": pa.array(out["min_temp_f_high"], pa.int16()),
            "plant_min_zone_derived": pa.array(out["plant_min_zone_derived"], pa.string()),
            "shade_tolerance": pa.array(out["shade_tolerance"], pa.string()),
            "drought_tolerance": pa.array(out["drought_tolerance"], pa.string()),
            "mature_height_ft_low": pa.array(out["mature_height_ft_low"], pa.float64()),
            "mature_height_ft_high": pa.array(out["mature_height_ft_high"], pa.float64()),
            "bloom_period": pa.array(out["bloom_period"], pa.string()),
            "growth_rate": pa.array(out["growth_rate"], pa.string()),
        }
    )
    long_rows.sort()
    chars = pa.table(
        {
            "symbol": pa.array([r[0] for r in long_rows], pa.string()),
            "category": pa.array([r[1] for r in long_rows], pa.string()),
            "characteristic": pa.array([r[2] for r in long_rows], pa.string()),
            "value": pa.array([r[3] for r in long_rows], pa.string()),
        }
    )
    report = {
        "checklist_rows": len(rows),
        "accepted": sum(1 for r in rows.values() if r["is_accepted"]),
        "with_characteristics": len(per_plant),
        "with_min_temp": sum(1 for v in out["min_temp_f_high"] if v is not None),
        "characteristic_values": len(long_rows),
        "unmatched_plant_ids": len(unmatched_ids),
        "plants_raw_sha256": {p.name: sha256(p) for p in sorted(RAW.glob("*")) if p.suffix in (".txt", ".json")},
    }
    return plants, chars, report


def main() -> None:
    """Fetch (when needed) and write the bundle."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--offline", action="store_true", help="only use files already in .cache/raw")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    zips, zip_sums = build_zip_zones(args.offline)
    plants, chars, report = build_plants(args.offline)
    opts = {"compression": "zstd", "compression_level": 19}
    pq.write_table(zips, OUT / "zip_zones.parquet", **opts)
    pq.write_table(plants, OUT / "plants.parquet", **opts)
    pq.write_table(chars, OUT / "plant_characteristics.parquet", **opts)
    report["zip_rows"] = zips.num_rows
    report["phzm_raw_sha256"] = zip_sums
    report["bundle_sha256"] = {p.name: sha256(p) for p in sorted(OUT.glob("*.parquet"))}
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
