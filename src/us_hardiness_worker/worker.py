"""Assemble the hardiness-zone and plant functions into a catalog and run the worker.

Run it:

    vgi-us-hardiness-worker                      # stdio: what DuckDB launches by default
    vgi-us-hardiness-worker --http --port 8000   # HTTP: for Docker / hosted deploys

Attach it from DuckDB:

    ATTACH 'us_hardiness' (TYPE vgi, LOCATION '/abs/path/to/.venv/bin/vgi-us-hardiness-worker');
    ATTACH 'us_hardiness' (TYPE vgi, LOCATION '...', cache_dir '/data/phzm-cache');
"""

from __future__ import annotations

import json
from typing import Annotated, Any

import pyarrow as pa
from vgi import Worker
from vgi.catalog import Catalog, Schema, Table
from vgi.catalog.attach_option import AttachOption
from vgi.catalog.constraints import constraint_record

from .grid import CacheInfo, ZoneForPoint
from .meta import (
    CATALOG_COMMENT,
    CATALOG_NAME,
    CATALOG_TAGS,
    SCHEMA_CATEGORIES,
    SCHEMA_COMMENT,
    SCHEMA_DOC_LLM,
    SCHEMA_DOC_MD,
    SOURCE_URL,
    column_comments,
    keywords_array,
)
from .plants import PlantCharacteristics, Plants, PlantsForZone
from .zip_zones import ZipZones, ZoneForZip
from .zones import ZoneForTemperature, ZoneForTemperatureUnit, Zones

FUNCTIONS: list[type[Any]] = [
    ZoneForZip,
    ZoneForPoint,
    ZoneForTemperature,
    ZoneForTemperatureUnit,
    PlantsForZone,
]
TABLE_SOURCES: list[type[Any]] = [Zones, ZipZones, Plants, PlantCharacteristics, CacheInfo]


def _stamp_example_queries(functions: list[type[Any]]) -> list[dict[str, str]]:
    """Publish each object's examples as ``vgi.example_queries`` (with descriptions).

    Overloads share one SQL name, so their examples are merged per name.
    Returns every example, for the schema-level list.
    """
    by_name: dict[str, list[dict[str, str]]] = {}
    for fn in functions:
        bucket = by_name.setdefault(fn.Meta.name, [])
        for ex in getattr(fn.Meta, "examples", []):
            entry = {"description": ex.description, "sql": ex.sql}
            if entry not in bucket:
                bucket.append(entry)
    for fn in functions:
        fn.Meta.tags = {**fn.Meta.tags, "vgi.example_queries": json.dumps(by_name[fn.Meta.name])}
    return [entry for entries in by_name.values() for entry in entries]


_ALL_EXAMPLES = _stamp_example_queries([*FUNCTIONS, *TABLE_SOURCES])


def _pk(table: str, cols: list[str]) -> tuple[Any, ...]:
    """PRIMARY KEY plus NOT NULL records, attested for every row of the bundled data."""
    return (
        constraint_record(
            ["main"], table, "pk", "PRIMARY_KEY", [[c] for c in cols], enforcement="ENFORCED", validation="ALL_ROWS"
        ),
        *(
            constraint_record(
                ["main"], table, f"{c}_not_null", "NOT_NULL", [[c]], enforcement="ENFORCED", validation="ALL_ROWS"
            )
            for c in cols
        ),
    )


def _table(source: type[Any], keys: list[str]) -> Table:
    schema: pa.Schema = source.FIXED_SCHEMA
    return Table(
        name=source.Meta.name,
        function=source,
        comment=source.Meta.description,
        tags={**source.Meta.tags, "domain": "agriculture"},
        column_comments=column_comments(schema),
        constraints=_pk(source.Meta.name, keys),
    )


TABLES = [
    _table(Zones, ["half_zone"]),
    _table(ZipZones, ["zip"]),
    _table(Plants, ["symbol"]),
    _table(PlantCharacteristics, ["symbol", "characteristic", "value"]),
    _table(CacheInfo, ["region"]),
]


class UsHardinessWorker(Worker):
    """The worker process: one catalog, one ``main`` schema."""

    catalog = Catalog(
        name=CATALOG_NAME,
        comment=CATALOG_COMMENT,
        source_url=SOURCE_URL,
        tags=CATALOG_TAGS,
        schemas=[
            Schema(
                path=["main"],
                comment=SCHEMA_COMMENT,
                functions=FUNCTIONS,
                tables=TABLES,
                tags={
                    "vgi.title": "Hardiness Zones and Plants",
                    "domain": "agriculture",
                    "vgi.keywords": keywords_array(
                        "hardiness zone, zip code, coordinates, minimum temperature, usda plants, native plants"
                    ),
                    "vgi.doc_llm": SCHEMA_DOC_LLM,
                    "vgi.doc_md": SCHEMA_DOC_MD,
                    "vgi.categories": json.dumps(SCHEMA_CATEGORIES),
                    "vgi.example_queries": json.dumps(_ALL_EXAMPLES),
                },
            )
        ],
    )

    class AttachOptions:
        """Options for ``ATTACH 'us_hardiness' (TYPE vgi, LOCATION ..., cache_dir '...', grid_url '...')``."""

        cache_dir: Annotated[
            str,
            AttachOption(
                desc="Directory for downloaded PRISM temperature grids; empty (default) uses the per-user cache "
                "directory (e.g. ~/.cache/vgi-us-hardiness)."
            ),
        ] = ""
        grid_url: Annotated[
            str,
            AttachOption(
                desc="Where zone_for_point fetches phzm_<region>_grid_2023.zip: an http(s) base URL or a local "
                "directory; empty (default) uses the PRISM site."
            ),
        ] = ""


def main() -> None:
    """Console-script entry point: parse ``--http`` / ``--unix`` / logging flags and serve."""
    UsHardinessWorker.main()


if __name__ == "__main__":
    main()
