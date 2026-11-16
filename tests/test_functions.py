"""Unit tests. The SQL-level tests live in test/sql and run through DuckDB."""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from vgi.arguments import Arguments
from vgi.client import Client

from us_hardiness_worker.data import bundled
from us_hardiness_worker.grid import (
    REGIONS,
    GridConfig,
    cache_path,
    fetch_grid,
    is_default_source,
    lookup_points,
    parse_header,
    route,
)
from us_hardiness_worker.plants import plants_for_zone
from us_hardiness_worker.zip_zones import lookup_zips, normalize_zips
from us_hardiness_worker.zones import (
    HALF_ZONES,
    half_zone_index,
    parse_zone,
    zones_for_temperatures,
)

SCHEMA = ["main"]
GRID_FIXTURES = Path(__file__).resolve().parent.parent / "test" / "fixtures" / "grids"


def _table(client: Client, name: str, *positional: Any, **named: Any) -> pa.Table:
    arguments = Arguments(
        positional=tuple(pa.scalar(v) for v in positional),
        named={k: pa.scalar(v) for k, v in named.items()},
    )
    batches = list(client.table_function(function_name=name, schema_path=SCHEMA, arguments=arguments))
    return pa.Table.from_batches(batches)


# ---------------------------------------------------------------- zone arithmetic


def test_half_zone_bounds_are_lower_inclusive() -> None:
    assert HALF_ZONES[0] == "1a" and HALF_ZONES[-1] == "13b" and len(HALF_ZONES) == 26
    assert half_zone_index(-60) == 0
    assert half_zone_index(-60.01) is None
    assert half_zone_index(0) == HALF_ZONES.index("7a")
    assert half_zone_index(-0.01) == HALF_ZONES.index("6b")
    assert half_zone_index(69.99) == 25
    assert half_zone_index(70) is None
    assert half_zone_index(float("nan")) is None


def test_zones_for_temperatures_vectorized() -> None:
    got = zones_for_temperatures(pa.array([-61.0, -60.0, 0.0, 4.99, 5.0, None, 70.0, 65.0]))
    assert got.to_pylist() == [None, "1a", "7a", "7a", "7b", None, None, "13b"]


def test_parse_zone() -> None:
    assert parse_zone("7a") == HALF_ZONES.index("7a")
    assert parse_zone(" 10B ") == HALF_ZONES.index("10b")
    assert parse_zone("6") == HALF_ZONES.index("6a")
    with pytest.raises(ValueError, match="unknown hardiness zone"):
        parse_zone("14a")


# ---------------------------------------------------------------- ZIP lookups


def test_normalize_zips() -> None:
    got = normalize_zips(pa.array(["97201", "97201-1234", "501", "ABCDE", None, " 10001 ", "1234567"]))
    assert got.to_pylist() == ["97201", "97201", "00501", None, None, "10001", None]


def test_lookup_zips_known_values() -> None:
    got: list[Any] = lookup_zips(pa.array(["10001", "00501", "99501", "00000", None])).to_pylist()  # type: ignore[misc]
    assert got[0] == {"zone": 7, "half_zone": "7b", "min_temp_f_low": 5, "min_temp_f_high": 10, "region": "conus"}
    assert got[1] is not None and got[1]["half_zone"] == "7b"
    assert got[2] is not None and got[2]["region"] == "alaska"
    assert got[3] is None and got[4] is None


def test_zip_bundle_integrity() -> None:
    t = bundled("zip_zones")
    assert t.num_rows == 40_502
    assert len(set(t.column("zip").to_pylist())) == t.num_rows
    assert set(t.column("region").to_pylist()) == {"conus", "alaska", "hawaii", "puerto_rico"}


# ---------------------------------------------------------------- grids


def test_route_regions_and_antimeridian() -> None:
    assert route(45.5, -122.7).name == "conus"  # type: ignore[union-attr]
    assert route(61.2, -149.9).name == "alaska"  # type: ignore[union-attr]
    assert route(52.85, 173.18).name == "alaska"  # type: ignore[union-attr]  # Attu, east of 180
    assert route(21.3, -157.9).name == "hawaii"  # type: ignore[union-attr]
    assert route(18.47, -66.1).name == "puerto_rico"  # type: ignore[union-attr]
    assert route(-33.9, 151.2) is None


def test_parse_header_rejects_other_layouts() -> None:
    good = (
        "BYTEORDER I\nLAYOUT BIL\nNROWS 2\nNCOLS 3\nNBANDS 1\nNBITS 32\nPIXELTYPE FLOAT\n"
        "ULXMAP 0\nULYMAP 0\nXDIM 1\nYDIM 1\n"
    )
    assert parse_header(good).ncols == 3
    with pytest.raises(ValueError, match="BYTEORDER"):
        parse_header(good.replace("BYTEORDER I", "BYTEORDER M"))


def test_lookup_points_from_fixture_grids(tmp_path: Path) -> None:
    cfg = GridConfig(cache_dir=str(tmp_path), grid_url=str(GRID_FIXTURES))
    assert not is_default_source(cfg)
    got: list[Any] = lookup_points(  # type: ignore[misc]
        [45.5152, 61.2181, 21.3069, 18.4655, None, 30.0, 40.75],
        [-122.6784, -149.9003, -157.8583, -66.1057, 0.0, -140.0, -73.99],
        cfg,
    ).to_pylist()
    assert got[0] == {"half_zone": "9a", "zone": 9, "min_temp_f": 22.0, "min_temp_c": -5.56, "region": "conus"}
    assert got[1] is not None and got[1]["half_zone"] == "5b" and got[1]["region"] == "alaska"
    assert got[2] is not None and got[2]["half_zone"] == "12b"
    assert got[3] is not None and got[3]["half_zone"] == "13b"
    assert got[4] is None  # NULL input
    assert got[5] is None  # Pacific Ocean, on no grid
    assert got[6] is None  # NYC lies outside the cropped CONUS fixture window
    assert (cache_path(cfg) / "phzm_us_grid_2023.bil").exists()


def test_fetch_grid_rejects_a_broken_zip(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    with zipfile.ZipFile(src / "phzm_hi_grid_2023.zip", "w") as zf:
        zf.writestr(
            "phzm_hi_grid_2023.hdr",
            "BYTEORDER I\nLAYOUT BIL\nNROWS 2\nNCOLS 2\nNBANDS 1\nNBITS 32\n"
            "PIXELTYPE FLOAT\nULXMAP 0\nULYMAP 0\nXDIM 1\nYDIM 1\n",
        )
        zf.writestr("phzm_hi_grid_2023.bil", b"\x00" * 12)  # 3 cells instead of 4
    cfg = GridConfig(cache_dir=str(tmp_path / "cache"), grid_url=str(src))
    hi = next(r for r in REGIONS if r.code == "hi")
    with pytest.raises(RuntimeError, match="does not match its header"):
        fetch_grid(hi, cfg)
    assert not (cache_path(cfg) / "phzm_hi_grid_2023.hdr").exists()


# ---------------------------------------------------------------- plants


def test_plants_bundle_and_derived_zone() -> None:
    t = bundled("plants")
    assert t.num_rows > 90_000
    acru = next(r for r in t.to_pylist() if r["symbol"] == "ACRU")
    assert acru["min_temp_f_high"] == -43 and acru["plant_min_zone_derived"] == "2b"
    assert "Tree" in acru["growth_habit"] and "MN" in acru["states"]
    assert len(set(t.column("symbol").to_pylist())) == t.num_rows


def test_plants_for_zone_filters() -> None:
    all_7b = plants_for_zone("7b", None, False)
    cold = plants_for_zone("3a", None, False)
    assert 0 < cold.num_rows < all_7b.num_rows
    assert set(cold.column("plant_min_zone_derived").to_pylist()) <= {"1a", "1b", "2a", "2b", "3a"}
    mn = plants_for_zone("4a", "mn", True)
    assert 0 < mn.num_rows < plants_for_zone("4a", "MN", False).num_rows
    for row in mn.select(["native_status"]).to_pylist():
        assert any(n["region"] == "L48" and n["status"] in ("Native", "Probably Native") for n in row["native_status"])


# ---------------------------------------------------------------- through the worker


def test_zones_table_through_worker(client: Client) -> None:
    t = _table(client, "zones")
    assert t.num_rows == 26
    assert t.column("min_temp_f_low").to_pylist()[:2] == [-60, -55]


def test_plants_for_zone_through_worker(client: Client) -> None:
    t = _table(client, "plants_for_zone", "5a", state="MN", native_only=True)
    assert t.num_rows == plants_for_zone("5a", "MN", True).num_rows


def test_plants_for_zone_rejects_bad_zone(client: Client) -> None:
    with pytest.raises(Exception, match="pattern"):
        _table(client, "plants_for_zone", "zone 5")
