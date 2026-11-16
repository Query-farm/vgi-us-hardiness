"""USDA PLANTS data: the ``plants`` and ``plant_characteristics`` tables and ``plants_for_zone``."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from typing import Annotated, ClassVar

import pyarrow as pa
import pyarrow.compute as pc
from vgi.arguments import Arg
from vgi.metadata import FunctionExample
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
from .meta import PLANTS_DERIVED_NOTE, PLANTS_URL, column, object_tags, result_columns
from .zones import HALF_ZONE_INDEX, parse_zone

NATIVE_TYPE = pa.list_(pa.struct([("region", pa.string()), ("status", pa.string())]))

_COLS: dict[str, tuple[pa.DataType, str, bool]] = {
    "symbol": (pa.string(), "USDA PLANTS symbol, the join key (e.g. 'ACRU' for red maple).", False),
    "accepted_symbol": (
        pa.string(),
        "Symbol of the accepted name; equals symbol for accepted names, differs for synonyms.",
        False,
    ),
    "is_accepted": (pa.bool_(), "True for an accepted name, false for a synonym.", False),
    "scientific_name": (pa.string(), "Scientific name with author, as in the PLANTS checklist.", True),
    "scientific_name_plain": (
        pa.string(),
        "Scientific name without author (only for plants that have characteristics).",
        True,
    ),
    "common_name": (pa.string(), "PLANTS common name; NULL when none is recorded.", True),
    "family": (pa.string(), "Plant family (synonyms inherit their accepted name's family).", True),
    "has_characteristics": (
        pa.bool_(),
        "True for the ~2,200 accepted taxa with PLANTS characteristics; the columns below are NULL otherwise.",
        False,
    ),
    "plant_group": (pa.string(), "PLANTS group: Dicot, Monocot, Gymnosperm, Fern, Horsetail or Lycopod.", True),
    "growth_habit": (
        pa.list_(pa.string()),
        "Growth habits, e.g. ['Tree'] or ['Shrub', 'Subshrub'] (Forb/herb, Graminoid, Shrub, Subshrub, Tree, Vine).",
        True,
    ),
    "duration": (pa.list_(pa.string()), "Life durations: Annual, Biennial and/or Perennial.", True),
    "native_status": (
        NATIVE_TYPE,
        "Nativity per PLANTS region: `STRUCT`(region, status) with region L48 (lower 48), AK, HI, PR, VI, CAN, GL, "
        "PB, NAV, SPM and status Native, Introduced, Probably Native, Probably Introduced or Waif.",
        True,
    ),
    "states": (
        pa.list_(pa.string()),
        "USPS codes of US states and territories where PLANTS records the plant.",
        True,
    ),
    "min_temp_f_low": (
        pa.int32(),
        "Low end of the PLANTS 'Temperature, Minimum (°F)' range (PLANTS publishes ranges, e.g. -47 to -43).",
        True,
    ),
    "min_temp_f_high": (
        pa.int32(),
        "High end of the PLANTS 'Temperature, Minimum (°F)' range: the plant tolerates at least this cold.",
        True,
    ),
    "plant_min_zone_derived": (
        pa.string(),
        "DERIVED, not USDA-published: zone_for_temperature(min_temp_f_high), clamped to '1a' for plants "
        "hardier than -60 °F. The coldest half zone the plant is documented to tolerate.",
        True,
    ),
    "shade_tolerance": (pa.string(), "PLANTS shade tolerance: Intolerant, Intermediate or Tolerant.", True),
    "drought_tolerance": (pa.string(), "PLANTS drought tolerance: None, Low, Medium or High.", True),
    "mature_height_ft_low": (pa.float64(), "Low end of the PLANTS 'Height at Maturity (feet)' range.", True),
    "mature_height_ft_high": (pa.float64(), "High end of the PLANTS 'Height at Maturity (feet)' range.", True),
    "bloom_period": (pa.string(), "PLANTS bloom period, e.g. 'Early Spring', 'Late Summer'.", True),
    "growth_rate": (pa.string(), "PLANTS growth rate: Slow, Moderate or Rapid.", True),
}

PLANTS_SCHEMA = pa.schema([column(name, t, c, nullable=n) for name, (t, c, n) in _COLS.items()])

CHARACTERISTICS_SCHEMA = pa.schema(
    [
        column("symbol", pa.string(), "USDA PLANTS symbol of the accepted taxon.", nullable=False),
        column(
            "category",
            pa.string(),
            "PLANTS characteristic group: Growth Requirements, Morphology, Reproduction or Suitability Use.",
            nullable=False,
        ),
        column(
            "characteristic",
            pa.string(),
            "Characteristic name as PLANTS shows it, e.g. 'Temperature, Minimum (°F)', 'Flower Color'.",
            nullable=False,
        ),
        column(
            "value",
            pa.string(),
            "Value as PLANTS publishes it: a category ('High', 'Yes') or a range label ('-47 - -43'); PLANTS "
            "uses a not-applicable marker where it recorded no value.",
            nullable=False,
        ),
    ]
)

_PLANTS_SOURCE = (
    f"Source: USDA NRCS PLANTS Database ({PLANTS_URL}), public domain; checklist plus the characteristics "
    "search service, bundled."
)


@dataclass(slots=True, frozen=True, kw_only=True)
class NoArgs:
    """The table scan takes no arguments."""


@init_single_worker
@bind_fixed_schema
class Plants(TableFunctionGenerator[NoArgs, OffsetCursor]):
    """The USDA PLANTS checklist, enriched with characteristics where PLANTS has them."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = PLANTS_SCHEMA

    class Meta:
        """Function metadata surfaced to DuckDB, agents and vgi-lint."""

        name = "plants"
        description = (
            "The USDA PLANTS checklist (~93,000 accepted names and synonyms) with habit, nativity, states, "
            "minimum temperature and a DERIVED minimum hardiness zone for ~2,200 taxa with characteristics"
        )
        tags = {
            **object_tags(
                title="USDA PLANTS Checklist",
                category="plants",
                keywords="plants, species, checklist, usda plants, scientific name, common name, native plants, "
                "minimum temperature, plant hardiness",
                doc_llm=(
                    "One row per PLANTS symbol from the USDA NRCS PLANTS complete checklist: accepted names "
                    "(`is_accepted`) and synonyms (`accepted_symbol` points to the accepted name), with "
                    "`scientific_name` (with author), `common_name` and `family`. For the ~2,200 accepted taxa "
                    "where PLANTS publishes characteristics (`has_characteristics`), the row also has "
                    "`plant_group`, `growth_habit` and `duration` (lists), `native_status` (list of "
                    "`STRUCT`(region, status), e.g. region 'L48' = lower 48 states), `states` (USPS codes), the "
                    "PLANTS minimum-temperature range `min_temp_f_low`..`min_temp_f_high`, "
                    "`plant_min_zone_derived`, shade/drought tolerance, mature height range, bloom period and "
                    "growth rate; for all other rows these are NULL (PLANTS does not publish them in bulk). "
                    "Filter with `list_contains(growth_habit, 'Tree')`, `list_contains(states, 'MN')`. "
                    + PLANTS_DERIVED_NOTE
                    + " "
                    + _PLANTS_SOURCE
                ),
                doc_md=(
                    "## plants\n\nThe USDA PLANTS checklist, keyed by PLANTS symbol, with characteristics for "
                    "the taxa PLANTS documents.\n\n"
                    f"> {PLANTS_DERIVED_NOTE}"
                ),
            ),
            "vgi.result_columns_schema": result_columns(PLANTS_SCHEMA),
            "vgi.executable_examples": json.dumps(
                [
                    {
                        "description": "Red maple's PLANTS minimum temperature and derived zone",
                        "sql": "SELECT symbol, common_name, min_temp_f_low, min_temp_f_high, plant_min_zone_derived "
                        "FROM us_hardiness.main.plants WHERE symbol = 'ACRU'",
                        "expected_result": [["ACRU", "red maple", -47, -43, "2b"]],
                    }
                ]
            ),
        }
        examples = [
            FunctionExample(
                sql="SELECT family, count(*) AS accepted_taxa FROM us_hardiness.main.plants WHERE is_accepted "
                "GROUP BY family ORDER BY accepted_taxa DESC LIMIT 5",
                description="The five plant families with the most accepted taxa",
            ),
            FunctionExample(
                sql="SELECT plant_min_zone_derived, count(*) AS trees FROM us_hardiness.main.plants "
                "WHERE list_contains(growth_habit, 'Tree') AND plant_min_zone_derived IS NOT NULL "
                "GROUP BY plant_min_zone_derived ORDER BY trees DESC LIMIT 5",
                description="How many documented trees have each derived minimum zone",
            ),
        ]

    @classmethod
    def cardinality(cls, params: BindParams[NoArgs]) -> TableCardinality:
        """Exact row count of the bundled checklist."""
        n = bundled("plants").num_rows
        return TableCardinality(estimate=n, max=n)

    @classmethod
    def initial_state(cls, params: ProcessParams[NoArgs]) -> OffsetCursor:
        """Start at the first plant."""
        return OffsetCursor()

    @classmethod
    def process(cls, params: ProcessParams[NoArgs], state: OffsetCursor, out: OutputCollector) -> None:
        """Emit one bounded batch per call."""
        table = bundled("plants")
        if state.offset >= table.num_rows:
            out.finish()
            return
        batch = slice_batch(table, state.offset, params.output_schema)
        out.emit(batch)
        state.offset += batch.num_rows


@init_single_worker
@bind_fixed_schema
class PlantCharacteristics(TableFunctionGenerator[NoArgs, OffsetCursor]):
    """Every PLANTS characteristic value, one row per (plant, characteristic, value)."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = CHARACTERISTICS_SCHEMA

    class Meta:
        """Function metadata surfaced to DuckDB, agents and vgi-lint."""

        name = "plant_characteristics"
        description = (
            "All USDA PLANTS characteristics (~83 attributes such as flower color, pH range, toxicity) for "
            "~2,200 taxa, one row per plant, characteristic and value"
        )
        tags = {
            **object_tags(
                title="PLANTS Characteristics",
                category="plants",
                keywords="plant characteristics, attributes, flower color, soil ph, toxicity, growth requirements, "
                "morphology",
                doc_llm=(
                    "Long-format USDA PLANTS characteristics: `symbol`, `category` (Growth Requirements, "
                    "Morphology, Reproduction, Suitability Use), `characteristic` (e.g. 'Flower Color', "
                    "'pH, Minimum', 'Temperature, Minimum (°F)', 'Toxicity') and `value` exactly as PLANTS "
                    "publishes it: categories like 'High' / 'Yes' or range labels like '5.1-5.2' and "
                    "'-47 - -43'; a not-applicable marker means PLANTS recorded no value. A characteristic can "
                    "have several values "
                    "for a plant (e.g. several flower colors). Join to `plants` on `symbol`. The most used "
                    "characteristics are also typed columns of `plants`. " + _PLANTS_SOURCE
                ),
                doc_md=(
                    "## plant_characteristics\n\nEvery characteristic USDA PLANTS publishes for a taxon, one row "
                    "per value. Pivot or filter it for attributes the `plants` table does not surface."
                ),
            ),
            "vgi.result_columns_schema": result_columns(CHARACTERISTICS_SCHEMA),
            "vgi.executable_examples": json.dumps(
                [
                    {
                        "description": "Red maple's flower color and shade tolerance",
                        "sql": "SELECT characteristic, value FROM us_hardiness.main.plant_characteristics "
                        "WHERE symbol = 'ACRU' AND characteristic IN ('Flower Color', 'Shade Tolerance') "
                        "ORDER BY characteristic",
                        "expected_result": [["Flower Color", "Red"], ["Shade Tolerance", "Intermediate"]],
                    }
                ]
            ),
        }
        examples = [
            FunctionExample(
                sql="SELECT value AS flower_color, count(*) AS taxa FROM us_hardiness.main.plant_characteristics "
                "WHERE characteristic = 'Flower Color' GROUP BY value ORDER BY taxa DESC",
                description="How many documented taxa have each flower color",
            ),
        ]

    @classmethod
    def cardinality(cls, params: BindParams[NoArgs]) -> TableCardinality:
        """Exact row count of the bundled characteristics."""
        n = bundled("plant_characteristics").num_rows
        return TableCardinality(estimate=n, max=n)

    @classmethod
    def initial_state(cls, params: ProcessParams[NoArgs]) -> OffsetCursor:
        """Start at the first row."""
        return OffsetCursor()

    @classmethod
    def process(cls, params: ProcessParams[NoArgs], state: OffsetCursor, out: OutputCollector) -> None:
        """Emit one bounded batch per call."""
        table = bundled("plant_characteristics")
        if state.offset >= table.num_rows:
            out.finish()
            return
        batch = slice_batch(table, state.offset, params.output_schema)
        out.emit(batch)
        state.offset += batch.num_rows


# PLANTS nativity regions a US state or territory belongs to.
STATE_REGION = {"AK": "AK", "HI": "HI", "PR": "PR", "VI": "VI", "GU": "PB", "PW": "PB", "UM": "PB"}
NATIVE_STATUSES = ("Native", "Probably Native")
US_REGIONS = ("L48", "AK", "HI", "PR", "VI", "PB", "NAV")

_FOR_ZONE_COLS = [
    "symbol",
    "scientific_name",
    "common_name",
    "family",
    "growth_habit",
    "duration",
    "plant_min_zone_derived",
    "min_temp_f_low",
    "min_temp_f_high",
    "native_status",
    "shade_tolerance",
    "drought_tolerance",
    "mature_height_ft_low",
    "mature_height_ft_high",
    "bloom_period",
]
FOR_ZONE_SCHEMA = pa.schema(
    [
        column(n, _COLS[n][0], _COLS[n][1], nullable=_COLS[n][2] and n != "plant_min_zone_derived")
        for n in _FOR_ZONE_COLS
    ]
)


@dataclass(slots=True, frozen=True, kw_only=True)
class ForZoneArgs:
    """Arguments for ``plants_for_zone(zone, state := NULL, native_only := false)``."""

    zone: Annotated[
        str,
        Arg(
            0,
            doc="Hardiness zone to plant in: a half zone like '6a' / '6b', or a whole zone '6' (treated as 6a, "
            "its colder half).",
            pattern="^\\s*(1[0-3]|[1-9])[abAB]?\\s*$",
        ),
    ]
    state: Annotated[
        str | None,
        Arg(
            "state",
            default=None,
            doc="USPS code of a US state or territory (e.g. 'MN'); only plants PLANTS records there. "
            "NULL (default) means anywhere.",
            pattern="^[A-Za-z]{2}$",
        ),
    ]
    native_only: Annotated[
        bool,
        Arg(
            "native_only",
            default=False,
            doc="When true, only plants Native or Probably Native to the PLANTS region of `state` (L48, AK, HI, "
            "PR, VI, Pacific Basin); without a state, native to any US region.",
        ),
    ]


def _is_native(native: list[dict[str, str]] | None, regions: tuple[str, ...]) -> bool:
    return any(n["region"] in regions and n["status"] in NATIVE_STATUSES for n in native or [])


@cache
def _with_zone_index() -> pa.Table:
    """Return accepted plants with a derived zone, plus its ordinal for comparisons."""
    t = bundled("plants")
    t = t.filter(pc.and_(t.column("is_accepted"), pc.is_valid(t.column("plant_min_zone_derived"))))
    idx = [HALF_ZONE_INDEX[str(z)] for z in t.column("plant_min_zone_derived").to_pylist()]
    return t.append_column("_zone_index", pa.array(idx, pa.int32()))


def plants_for_zone(zone: str, state: str | None, native_only: bool) -> pa.Table:
    """Return accepted plants whose derived minimum zone is ``zone`` or colder, by state and nativity."""
    target = parse_zone(zone)
    t = _with_zone_index()
    t = t.filter(pc.less_equal(t.column("_zone_index"), pa.scalar(target, pa.int32())))
    st = state.strip().upper() if state else None
    if st is not None:
        t = t.filter(pa.array([st in (s or ()) for s in t.column("states").to_pylist()], pa.bool_()))
    if native_only:
        regions: tuple[str, ...] = (STATE_REGION.get(st, "L48"),) if st else US_REGIONS
        mask = [_is_native(n, regions) for n in t.column("native_status").to_pylist()]
        t = t.filter(pa.array(mask, pa.bool_()))
    return t.select(_FOR_ZONE_COLS).sort_by([("scientific_name", "ascending")])


@init_single_worker
@bind_fixed_schema
class PlantsForZone(TableFunctionGenerator[ForZoneArgs, OffsetCursor]):
    """Plants documented to tolerate a zone's winters, optionally limited to a state and to natives."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = FOR_ZONE_SCHEMA

    class Meta:
        """Function metadata surfaced to DuckDB, agents and vgi-lint."""

        name = "plants_for_zone"
        description = (
            "Accepted PLANTS taxa whose DERIVED minimum zone is the given zone or colder, optionally "
            "limited to a state and to native plants"
        )
        tags = {
            **object_tags(
                title="Plants for Zone",
                category="plants",
                keywords="plants for zone, what to plant, hardy plants, native plants, garden planning, "
                "cold hardiness, plant selection",
                doc_llm=(
                    "List the accepted USDA PLANTS taxa that tolerate the winters of a hardiness zone: those "
                    "whose `plant_min_zone_derived` is the same as or colder than `zone` (so zone '6a' includes "
                    "plants rated 1a..6a). `zone` is positional ('6a', '6b' or '6', which means 6a). Named "
                    "arguments: `state := 'MN'` keeps plants PLANTS records in that state; `native_only := true` "
                    "keeps plants Native or Probably Native to the state's PLANTS region (lower 48 = 'L48', "
                    "or AK, HI, PR, VI, Pacific Basin), or to any US region when no state is given. Only the "
                    "~2,100 taxa with a PLANTS minimum temperature can appear. Minimum temperature is the only "
                    "climate test: PLANTS has no heat or maximum zone, so warm-zone results include cold-climate "
                    "plants. Get the zone for a place first with `zone_for_zip(...).half_zone`. " + PLANTS_DERIVED_NOTE
                ),
                doc_md=(
                    "## plants_for_zone(zone, state := NULL, native_only := false)\n\n"
                    "Plants documented to survive a zone's winter lows, by the minimum temperature in USDA "
                    "PLANTS.\n\n"
                    f"> {PLANTS_DERIVED_NOTE}"
                ),
            ),
            "vgi.result_columns_schema": result_columns(FOR_ZONE_SCHEMA),
            "vgi.executable_examples": json.dumps(
                [
                    {
                        "description": "How many native trees in Minnesota tolerate zone 4a",
                        "sql": "SELECT count(*) AS trees FROM us_hardiness.main.plants_for_zone('4a', "
                        "state := 'MN', native_only := true) WHERE list_contains(growth_habit, 'Tree')",
                        "expected_result": [[95]],
                    }
                ]
            ),
        }
        examples = [
            FunctionExample(
                sql="SELECT scientific_name, common_name, mature_height_ft_high FROM "
                "us_hardiness.main.plants_for_zone(us_hardiness.main.zone_for_zip('55401').half_zone, "
                "state := 'MN', native_only := true) WHERE list_contains(growth_habit, 'Shrub') "
                "ORDER BY scientific_name LIMIT 10",
                description="Native shrubs for a Minneapolis ZIP code's zone",
            ),
            FunctionExample(
                sql="SELECT plant_min_zone_derived, count(*) AS plants FROM us_hardiness.main.plants_for_zone('7b') "
                "GROUP BY plant_min_zone_derived ORDER BY plants DESC LIMIT 5",
                description="Plants hardy in zone 7b, grouped by their derived minimum zone",
            ),
        ]

    @classmethod
    def cardinality(cls, params: BindParams[ForZoneArgs]) -> TableCardinality:
        """At most every plant with a derived zone."""
        return TableCardinality(estimate=500, max=_with_zone_index().num_rows)

    @classmethod
    def initial_state(cls, params: ProcessParams[ForZoneArgs]) -> OffsetCursor:
        """Start at the first matching plant."""
        return OffsetCursor()

    @classmethod
    def process(cls, params: ProcessParams[ForZoneArgs], state: OffsetCursor, out: OutputCollector) -> None:
        """Filter (cheap, in memory) and emit one bounded batch per call."""
        a = params.args
        table = plants_for_zone(a.zone, a.state, a.native_only)
        if state.offset >= table.num_rows:
            out.finish()
            return
        batch = slice_batch(table, state.offset, params.output_schema)
        out.emit(batch)
        state.offset += batch.num_rows
        if state.offset >= table.num_rows:
            out.finish()
