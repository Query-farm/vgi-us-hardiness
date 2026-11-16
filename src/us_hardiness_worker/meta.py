"""Discovery metadata: catalog/schema docs, categories and helpers shared by every object.

DuckDB, coding agents and the ``vgi-lint`` metadata checker all read these tags.
"""

from __future__ import annotations

import json
from typing import Any

import pyarrow as pa

CATALOG_NAME = "us_hardiness"

SOURCE_URL = "https://github.com/Query-farm/vgi-us-hardiness"
PHZM_URL = "https://prism.oregonstate.edu/phzm/"
USDA_PHZM_URL = "https://planthardiness.ars.usda.gov"
PLANTS_URL = "https://plants.usda.gov"

DISCLAIMER = (
    "Not the official USDA Plant Hardiness Zone Map: values are derived from the 2023 USDA PHZM GIS "
    "datasets (PRISM Group, Oregon State University); see planthardiness.ars.usda.gov for the official map."
)

PLANTS_DERIVED_NOTE = (
    "plant_min_zone_derived is DERIVED by this worker from the USDA PLANTS 'Temperature, Minimum (°F)' "
    "characteristic; neither USDA PLANTS nor the USDA hardiness map publishes a zone for a plant."
)

# Categories group functions in docs and agent tools. Every object's
# ``vgi.category`` must name one of these.
SCHEMA_CATEGORIES = [
    {
        "name": "zones",
        "title": "Hardiness Zones",
        "description": "Zone lookups by ZIP code, coordinates or temperature, and the zone legend.",
    },
    {
        "name": "plants",
        "title": "Plants",
        "description": "USDA PLANTS checklist, characteristics and plants suited to a zone.",
    },
    {
        "name": "cache",
        "title": "Data Cache",
        "description": "State of the locally cached temperature grids.",
    },
]


def keywords_array(keywords: str) -> str:
    """Turn ``"a, b, c"`` into the JSON array string ``vgi.keywords`` requires."""
    return json.dumps([k.strip() for k in keywords.split(",") if k.strip()])


def object_tags(*, title: str, category: str, keywords: str, doc_llm: str, doc_md: str) -> dict[str, str]:
    """Build the standard per-object discovery tags."""
    if category not in {c["name"] for c in SCHEMA_CATEGORIES}:
        raise ValueError(f"unknown category {category!r}; add it to SCHEMA_CATEGORIES")
    return {
        "vgi.title": title,
        "vgi.category": category,
        "vgi.keywords": keywords_array(keywords),
        "vgi.doc_llm": doc_llm,
        "vgi.doc_md": doc_md,
    }


def column(name: str, type_: pa.DataType, comment: str, *, nullable: bool = True) -> pa.Field[Any]:
    """Build an Arrow field whose ``comment`` shows up in DuckDB's ``DESCRIBE`` and agent tools."""
    return pa.field(name, type_, nullable=nullable, metadata={b"comment": comment.encode()})


def duckdb_type(t: pa.DataType) -> str:
    """Return the DuckDB SQL spelling of an Arrow type used in this worker."""
    if pa.types.is_string(t) or pa.types.is_large_string(t):
        return "VARCHAR"
    if pa.types.is_boolean(t):
        return "BOOLEAN"
    if pa.types.is_int8(t):
        return "TINYINT"
    if pa.types.is_int16(t):
        return "SMALLINT"
    if pa.types.is_int32(t):
        return "INTEGER"
    if pa.types.is_int64(t):
        return "BIGINT"
    if pa.types.is_float64(t):
        return "DOUBLE"
    if pa.types.is_timestamp(t):
        return "TIMESTAMP WITH TIME ZONE" if t.tz else "TIMESTAMP"
    if pa.types.is_list(t):
        return duckdb_type(t.value_type) + "[]"
    if pa.types.is_struct(t):
        return "STRUCT(" + ", ".join(f"{f.name} {duckdb_type(f.type)}" for f in t) + ")"
    raise TypeError(f"no DuckDB spelling for {t}")


def result_columns(schema: pa.Schema) -> str:
    """``vgi.result_columns_schema`` JSON for a table function's output schema."""
    return json.dumps(
        [
            {
                "name": f.name,
                "type": duckdb_type(f.type),
                "description": (f.metadata or {}).get(b"comment", b"").decode(),
            }
            for f in schema
        ]
    )


def column_comments(schema: pa.Schema) -> dict[str, str]:
    """Column comments of a schema, for a catalog ``Table``."""
    return {f.name: (f.metadata or {}).get(b"comment", b"").decode() for f in schema}


CATALOG_COMMENT = (
    "USDA 2023 plant hardiness zones by ZIP code, coordinates or temperature, and USDA PLANTS data with "
    "derived plant zones. Not the official USDA Plant Hardiness Zone Map: derived from the PRISM/Oregon "
    "State GIS datasets."
)

CATALOG_DOC_LLM = (
    "Answers 'what hardiness zone is this place?' and 'which plants survive winters here?' from public US "
    "government data, offline. Zones come from the 2023 USDA Plant Hardiness Zone Map GIS datasets "
    "published by the PRISM Group at Oregon State University: the official ZIP-code listing (CONUS, "
    "Alaska, Hawaii, Puerto Rico) is bundled, and the 800 m / 400 m gridded 1991-2020 mean annual extreme "
    "minimum temperature rasters are downloaded on first use into a local cache for point lookups. "
    "Zones are 10 °F bands numbered 1 to 13, each split into half zones 'a' (colder) and 'b' (warmer) of "
    "5 °F, from 1a at -60 °F to 13b at 70 °F. Plant data comes from the USDA NRCS PLANTS Database: the "
    "complete checklist of ~93,000 accepted names and synonyms, plus the characteristics PLANTS records "
    "for about 2,200 accepted taxa (growth habit, duration, nativity by region, states, shade and drought "
    "tolerance, height, bloom period and the minimum temperature tolerated). PLANTS publishes numeric "
    "characteristics as ranges (for example -47 to -43 °F), so temperatures and heights are low/high "
    "pairs. " + PLANTS_DERIVED_NOTE + " Join keys: ZIP as 5-character `VARCHAR`, USPS state codes, PLANTS "
    "symbols. " + DISCLAIMER
)

CATALOG_DOC_MD = (
    "# US plant hardiness zones and plants\n\n"
    "**What hardiness zone is a ZIP code or a point in, and which plants tolerate its winters?** This "
    "[VGI](https://query.farm/vgi) worker answers both in DuckDB SQL from public data: the 2023 USDA Plant "
    f"Hardiness Zone Map GIS datasets from the [PRISM Group, Oregon State University]({PHZM_URL}) and the "
    f"[USDA NRCS PLANTS Database]({PLANTS_URL}).\n\n"
    "ZIP-code zones and the plant checklist are bundled, so they work offline. Point lookups read the "
    "1991-2020 mean annual extreme minimum temperature grid, downloaded once into a local cache the first "
    "time a point in that region is looked up.\n\n"
    f"> **{DISCLAIMER}**\n\n"
    f"> {PLANTS_DERIVED_NOTE}"
)

SCHEMA_COMMENT = (
    "Hardiness zone lookups (ZIP, point, temperature), the zone legend, and USDA PLANTS data with a "
    "derived plant minimum zone. " + DISCLAIMER
)

SCHEMA_DOC_LLM = (
    "Everything lives in `main`. To find the zone of a place, use a ZIP code (bundled official listing, "
    "fastest, offline) or a latitude/longitude (reads the PRISM temperature grid, downloaded to a cache on "
    "first use per region: about 41 MB for the contiguous US, 15 MB for Alaska, under 1 MB for Hawaii or "
    "Puerto Rico). Zone results carry the half zone (e.g. '7a'), the whole zone number and the "
    "temperature band. To relate plants to zones, use the PLANTS checklist keyed by PLANTS symbol; only "
    "plants with PLANTS characteristics have a minimum temperature and therefore a derived minimum zone. "
    "A plant is suited to a zone when its derived minimum zone is the same or colder. Set the ATTACH "
    "options `cache_dir` (where grids are stored) and `grid_url` (an alternate location of the grid "
    "zips, http(s) or a local directory) when needed. " + DISCLAIMER
)

SCHEMA_DOC_MD = (
    "## main\n\n"
    "- **Zones:** look up a hardiness zone by ZIP code, by coordinates, or by temperature.\n"
    "- **Plants:** the USDA PLANTS checklist and characteristics, with a minimum zone derived from each "
    "plant's minimum temperature.\n"
    "- **Cache:** where the downloaded temperature grids live.\n\n"
    f"> {DISCLAIMER}"
)

AGENT_TEST_TASKS = [
    {
        "name": "zone_7_celsius_band",
        "prompt": "What are the Celsius lower and upper bounds of hardiness half zone 7a?",
    },
    {
        "name": "honolulu_point_zone",
        "prompt": "What hardiness half zone is the point at latitude 21.3069, longitude -157.8583 (Honolulu) in?",
    },
    {
        "name": "red_maple_min_temp",
        "prompt": "According to USDA PLANTS, what is the minimum temperature range (°F) for red maple, symbol ACRU?",
    },
    {
        "name": "red_maple_flower_color",
        "prompt": "What flower color does USDA PLANTS record for symbol ACRU?",
    },
    {
        "name": "grid_regions",
        "prompt": "List the temperature-grid regions this worker can cache, alphabetically.",
    },
    {
        "name": "zone_of_zip",
        "prompt": "Using the us_hardiness worker, what USDA hardiness half zone is ZIP code 10001 in?",
    },
    {
        "name": "zone_of_temperature_celsius",
        "prompt": "Which hardiness half zone has a mean annual extreme minimum temperature of -20 °C?",
    },
    {
        "name": "zips_per_zone_alaska",
        "prompt": "How many Alaska ZIP codes are in each whole hardiness zone? Return zone and count.",
    },
    {
        "name": "native_trees_zone_4a_mn",
        "prompt": (
            "How many plants with growth habit 'Tree' are native to Minnesota's region and hardy to zone 4a "
            "(by this worker's derived plant minimum zone), counted for state MN?"
        ),
    },
]

CATALOG_TAGS = {
    "vgi.title": "US Plant Hardiness Zones",
    "vgi.subtitle": "USDA 2023 hardiness zones by ZIP, point or temperature, and USDA PLANTS data with derived zones.",
    "vgi.doc_llm": CATALOG_DOC_LLM,
    "vgi.doc_md": CATALOG_DOC_MD,
    "vgi.source_url": SOURCE_URL,
    "vgi.icon_url": "https://query.farm/media-kit/products/vgi/vgi-logo.png",
    "vgi.license": "0BSD",
    "vgi.author": "Query.Farm",
    "vgi.copyright": "Copyright 2026 Query Farm LLC - https://query.farm",
    "vgi.keywords": keywords_array(
        "plant hardiness zone, usda, phzm, prism, gardening, zip code, plants database, nrcs, "
        "minimum temperature, horticulture"
    ),
    "vgi.support_contact": f"{SOURCE_URL}/issues",
    "vgi.support_policy_url": f"{SOURCE_URL}#support",
    # Public analyst tasks for `vgi-lint simulate`; private graders live in vgi-agent-tests.yaml.
    "vgi.agent_test_tasks": json.dumps(AGENT_TEST_TASKS),
}
