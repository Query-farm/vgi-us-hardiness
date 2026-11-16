"""ZIP-code lookups from the official 2023 PHZM ZIP-code listing: ``zone_for_zip`` and ``zip_zones``."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Annotated, ClassVar

import pyarrow as pa
import pyarrow.compute as pc
from vgi.arguments import Param, Returns
from vgi.metadata import FunctionExample
from vgi.scalar_function import ScalarFunction
from vgi.table_function import (
    BindParams,
    OutputCollector,
    ProcessParams,
    TableCardinality,
    TableFunctionGenerator,
    bind_fixed_schema,
    init_single_worker,
)

from .data import OffsetCursor, bundled, slice_batch
from .meta import DISCLAIMER, column, object_tags, result_columns

ZIP_STRUCT = pa.struct(
    [
        pa.field("zone", pa.int32()),
        pa.field("half_zone", pa.string()),
        pa.field("min_temp_f_low", pa.int32()),
        pa.field("min_temp_f_high", pa.int32()),
        pa.field("region", pa.string()),
    ]
)

_ZIP_RE = re.compile(r"^\s*(\d{3,5})(?:-\d{4})?\s*$")


def normalize_zips(zips: pa.Array) -> pa.StringArray:  # type: ignore[type-arg]
    """Five-digit ZIPs: accept '97201', '97201-1234' and zero-pad 3-4 digit values ('501' -> '00501')."""
    out: list[str | None] = []
    for z in zips.to_pylist():
        m = _ZIP_RE.match(z) if isinstance(z, str) else None
        out.append(m.group(1).zfill(5) if m else None)
    return pa.array(out, pa.string())


def lookup_zips(zips: pa.Array) -> pa.StructArray:  # type: ignore[type-arg]
    """Vectorized ZIP -> zone struct; unknown or malformed ZIPs give NULL."""
    table = bundled("zip_zones")
    keys = normalize_zips(zips)
    idx = pc.index_in(keys, value_set=table.column("zip"))
    fields = [
        pc.cast(pc.take(table.column("zone"), idx), pa.int32()),
        pc.take(table.column("half_zone"), idx),
        pc.cast(pc.take(table.column("min_temp_f_low"), idx), pa.int32()),
        pc.cast(pc.take(table.column("min_temp_f_high"), idx), pa.int32()),
        pc.take(table.column("region"), idx),
    ]
    arrays = [f.combine_chunks() if isinstance(f, pa.ChunkedArray) else f for f in fields]
    mask = pc.is_null(idx)
    return pa.StructArray.from_arrays(arrays, fields=list(ZIP_STRUCT), mask=mask)


class ZoneForZip(ScalarFunction):
    """``zone_for_zip(zip)``: the 2023 hardiness zone of a US ZIP code."""

    class Meta:
        """Function metadata surfaced to DuckDB, agents and vgi-lint."""

        name = "zone_for_zip"
        description = (
            "2023 USDA hardiness zone of a US ZIP code as `STRUCT`(zone, half_zone, min_temp_f_low, "
            "min_temp_f_high, region); NULL for unknown ZIPs. " + DISCLAIMER
        )
        tags = object_tags(
            title="Zone for ZIP Code",
            category="zones",
            keywords="zip code, hardiness zone, zone lookup, postal code, gardening zone, half zone",
            doc_llm=(
                "Look up the 2023 USDA Plant Hardiness Zone of a US ZIP code in the bundled PRISM/Oregon State "
                "ZIP-code listing (about 40,500 ZIPs across the contiguous US, Alaska, Hawaii and Puerto Rico; "
                "offline). Returns a `STRUCT`: `zone` (whole zone, `INTEGER` 1-13), `half_zone` (e.g. '7a'), "
                "`min_temp_f_low` / `min_temp_f_high` (the half zone's °F band of mean annual extreme minimum "
                "temperature, low inclusive) and `region` ('conus', 'alaska', 'hawaii', 'puerto_rico'). Pass the "
                "ZIP as `VARCHAR`; '97201-1234' (ZIP+4) and 3-4 digit values (leading zeros lost, e.g. '501') are "
                "normalized. Unknown or malformed ZIPs return NULL. Read a field with "
                "`zone_for_zip('10001').half_zone`. " + DISCLAIMER
            ),
            doc_md=(
                "## zone_for_zip(zip)\n\n"
                "`zone_for_zip('10001')` returns `{zone: 7, half_zone: 7b, min_temp_f_low: 5, min_temp_f_high: "
                "10, region: conus}`. Offline, from the official 2023 ZIP-code listing.\n\n"
                f"> {DISCLAIMER}"
            ),
        )
        examples = [
            FunctionExample(
                sql="SELECT z AS zip, us_hardiness.main.zone_for_zip(z).half_zone AS half_zone "
                "FROM (VALUES ('10001'), ('97201'), ('99501'), ('96813')) v(z) ORDER BY z",
                description="Half zones of ZIPs in New York, Portland, Anchorage and Honolulu",
            ),
        ]

    @classmethod
    def compute(
        cls,
        zip: Annotated[pa.StringArray, Param(doc="US ZIP code, 5 digits with leading zeros (ZIP+4 accepted).")],  # noqa: A002
    ) -> Annotated[pa.StructArray, Returns(arrow_type=ZIP_STRUCT)]:
        """Return the zone struct for every ZIP."""
        return lookup_zips(zip)


ZIP_ZONES_SCHEMA = pa.schema(
    [
        column("zip", pa.string(), "Five-digit US ZIP code (VARCHAR, leading zeros kept).", nullable=False),
        column(
            "region",
            pa.string(),
            "Map region of the listing: 'conus', 'alaska', 'hawaii' or 'puerto_rico'.",
            nullable=False,
        ),
        column("zone", pa.int32(), "Whole hardiness zone number, 1 to 13 (each spans 10 °F).", nullable=False),
        column("half_zone", pa.string(), "Half zone, e.g. '7a' (colder half) or '7b'.", nullable=False),
        column(
            "min_temp_f_low",
            pa.int32(),
            "Lower bound (inclusive) of the half zone's mean annual extreme minimum temperature, °F.",
            nullable=False,
        ),
        column("min_temp_f_high", pa.int32(), "Upper bound (exclusive) of that band, °F.", nullable=False),
    ]
)


@dataclass(slots=True, frozen=True, kw_only=True)
class NoArgs:
    """The table scan takes no arguments."""


@init_single_worker
@bind_fixed_schema
class ZipZones(TableFunctionGenerator[NoArgs, OffsetCursor]):
    """Every ZIP code in the 2023 PHZM ZIP-code listing."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = ZIP_ZONES_SCHEMA

    class Meta:
        """Function metadata surfaced to DuckDB, agents and vgi-lint."""

        name = "zip_zones"
        description = (
            "Every US ZIP code with its 2023 USDA hardiness zone and °F band (about 40,500 rows). " + DISCLAIMER
        )
        tags = {
            **object_tags(
                title="ZIP Code Zones",
                category="zones",
                keywords="zip codes, zip code zones, hardiness by zip, zone listing, postal codes, phzm",
                doc_llm=(
                    "The whole 2023 USDA PHZM ZIP-code listing as a table: one row per ZIP (about 40,500) with "
                    "`region` ('conus', 'alaska', 'hawaii', 'puerto_rico'), `zone`, `half_zone` and the °F band. "
                    "Use it for set-based questions (how many ZIPs per zone, the warmest ZIPs in a region) or to "
                    "join a table of ZIPs (`ON zip_zones.zip = t.zip`, both `VARCHAR`). The listing has no "
                    "state column; the first three ZIP digits identify the sectional center. Bundled, offline. "
                    + DISCLAIMER
                ),
                doc_md=(
                    "## zip_zones\n\nThe official 2023 ZIP-code listing of hardiness zones, one row per ZIP.\n\n"
                    f"> {DISCLAIMER}"
                ),
            ),
            "vgi.result_columns_schema": result_columns(ZIP_ZONES_SCHEMA),
            "vgi.executable_examples": json.dumps(
                [
                    {
                        "description": "Puerto Rico ZIPs per whole zone",
                        "sql": "SELECT zone, count(*) AS zips FROM us_hardiness.main.zip_zones "
                        "WHERE region = 'puerto_rico' GROUP BY zone ORDER BY zone",
                        "expected_result": [[11, 2], [12, 48], [13, 126]],
                    }
                ]
            ),
        }
        examples = [
            FunctionExample(
                sql="SELECT half_zone, count(*) AS zips FROM us_hardiness.main.zip_zones "
                "WHERE region = 'conus' GROUP BY half_zone ORDER BY zips DESC LIMIT 5",
                description="The five most common half zones among contiguous-US ZIP codes",
            ),
        ]

    @classmethod
    def cardinality(cls, params: BindParams[NoArgs]) -> TableCardinality:
        """Exact row count of the bundled listing."""
        n = bundled("zip_zones").num_rows
        return TableCardinality(estimate=n, max=n)

    @classmethod
    def initial_state(cls, params: ProcessParams[NoArgs]) -> OffsetCursor:
        """Start at the first ZIP."""
        return OffsetCursor()

    @classmethod
    def process(cls, params: ProcessParams[NoArgs], state: OffsetCursor, out: OutputCollector) -> None:
        """Emit one bounded batch per call."""
        table = bundled("zip_zones")
        if state.offset >= table.num_rows:
            out.finish()
            return
        batch = slice_batch(table, state.offset, params.output_schema)
        out.emit(batch)
        state.offset += batch.num_rows
