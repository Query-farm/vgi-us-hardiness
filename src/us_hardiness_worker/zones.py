"""USDA Plant Hardiness Zone arithmetic, the ``zones`` table and ``zone_for_temperature``.

The 2023 USDA Plant Hardiness Zone Map divides the 1991-2020 mean annual extreme
minimum temperature into 13 zones of 10 °F, each split into half zones ``a`` and
``b`` of 5 °F. Zone 1a starts at -60 °F and 13b ends at 70 °F; a half zone
includes its lower bound and excludes its upper bound, so 0 °F is 7a and -0.1 °F
is 6b.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar

import pyarrow as pa
import pyarrow.compute as pc
from vgi.arguments import ConstParam, Param, Returns
from vgi.metadata import FunctionExample
from vgi.scalar_function import ScalarFunction
from vgi.table_function import (
    OutputCollector,
    ProcessParams,
    TableFunctionGenerator,
    bind_fixed_schema,
    init_single_worker,
)

from .meta import DISCLAIMER, column, object_tags, result_columns

MIN_F = -60.0
MAX_F = 70.0
STEP_F = 5.0
HALF_ZONE_COUNT = 26


def half_zone_name(index: int) -> str:
    """Name of half zone ``index`` (0 is ``1a``, 25 is ``13b``)."""
    return f"{index // 2 + 1}{'ab'[index % 2]}"


HALF_ZONES: list[str] = [half_zone_name(i) for i in range(HALF_ZONE_COUNT)]
HALF_ZONE_INDEX: dict[str, int] = {name: i for i, name in enumerate(HALF_ZONES)}


def half_zone_index(temp_f: float) -> int | None:
    """Index of the half zone containing ``temp_f`` (°F), or None outside -60..70 °F."""
    if temp_f != temp_f or temp_f < MIN_F or temp_f >= MAX_F:  # NaN or out of range
        return None
    return int((temp_f - MIN_F) // STEP_F)


def half_zone_low_f(index: int) -> int:
    """Lower (inclusive) °F bound of half zone ``index``."""
    return int(MIN_F + STEP_F * index)


def parse_zone(text: str) -> int:
    """Half-zone index for ``'7a'``, ``'7b'`` or a whole zone ``'7'`` (which means its colder half, 7a).

    Args:
        text: The zone as written by the caller.

    Returns:
        The half-zone index, 0 (1a) to 25 (13b).

    Raises:
        ValueError: The text is not a zone from 1 to 13, optionally followed by a or b.

    """
    t = text.strip().lower()
    if t in HALF_ZONE_INDEX:
        return HALF_ZONE_INDEX[t]
    if t.isdigit() and 1 <= int(t) <= 13:
        return HALF_ZONE_INDEX[f"{int(t)}a"]
    raise ValueError(f"unknown hardiness zone {text!r}: expected 1 to 13, optionally followed by a or b (e.g. '7a')")


def f_to_c(temp_f: float) -> float:
    """Convert °F to °C, rounded to two decimals."""
    return round((temp_f - 32.0) * 5.0 / 9.0, 2)


def zones_for_temperatures(temps_f: Any) -> pa.StringArray:
    """Vectorized half zone for a column of °F temperatures; NULL, NaN and out-of-range give NULL."""
    temps: Any = pc.cast(temps_f, pa.float64())
    idx: Any = pc.floor(pc.divide(pc.subtract(temps, MIN_F), STEP_F))
    in_range: Any = pc.and_kleene(pc.greater_equal(temps, pa.scalar(MIN_F)), pc.less(temps, pa.scalar(MAX_F)))
    idx_int: Any = pc.cast(pc.if_else(in_range, idx, pa.scalar(None, pa.float64())), pa.int64())
    names = pa.array(HALF_ZONES, pa.string())
    result: pa.StringArray = pc.take(names, idx_int)
    return result


_TEMP_F_DOC = "Mean annual extreme minimum temperature in °F; NULL in, NULL out."
_TEMP_DOC = "Mean annual extreme minimum temperature, in the unit named by `unit`; NULL in, NULL out."
_UNIT_DOC = "Temperature unit of `temp`: 'F' for Fahrenheit or 'C' for Celsius."


class ZoneForTemperature(ScalarFunction):
    """``zone_for_temperature(temp_f)``: the half zone for a °F temperature."""

    class Meta:
        """Function metadata surfaced to DuckDB, agents and vgi-lint."""

        name = "zone_for_temperature"
        description = (
            "USDA hardiness half zone ('1a'..'13b') for a mean annual extreme minimum temperature in °F; "
            "NULL outside -60..70 °F. " + DISCLAIMER
        )
        tags = object_tags(
            title="Zone for Temperature (°F)",
            category="zones",
            keywords="hardiness zone, temperature to zone, minimum temperature, fahrenheit, half zone, usda zone",
            doc_llm=(
                "Map a mean annual extreme minimum temperature in **°F** to its USDA Plant Hardiness half zone "
                "using the 2023 map's bands: zone 1a is -60 to -55 °F and each half zone spans 5 °F up to 13b "
                "(65 to 70 °F). The lower bound is inclusive: `zone_for_temperature(0)` is `'7a'`, "
                "`zone_for_temperature(-0.5)` is `'6b'`. Returns NULL for NULL input and for temperatures "
                "below -60 °F or at/above 70 °F, which fall outside the published zones. Use "
                "`zone_for_temperature(temp, 'C')` for Celsius. This is arithmetic on the published bands, "
                "not a lookup of the official map. " + DISCLAIMER
            ),
            doc_md=(
                "## zone_for_temperature(temp_f)\n\n"
                "The half zone for a temperature in °F: `zone_for_temperature(5)` is `'7b'`. Bands are 5 °F "
                "wide from 1a (-60 °F) to 13b (70 °F); outside that range the result is NULL.\n\n"
                f"> {DISCLAIMER}"
            ),
        )
        examples = [
            FunctionExample(
                sql="SELECT t AS temp_f, us_hardiness.main.zone_for_temperature(t) AS half_zone "
                "FROM (VALUES (-25.0), (0.0), (5.0), (42.5)) v(t) ORDER BY t",
                description="Half zones for a few winter low temperatures in °F",
            ),
        ]

    @classmethod
    def compute(
        cls,
        temp_f: Annotated[pa.DoubleArray, Param(doc=_TEMP_F_DOC)],
    ) -> Annotated[pa.StringArray, Returns()]:
        """Return the half zone of each temperature."""
        return zones_for_temperatures(temp_f)


class ZoneForTemperatureUnit(ScalarFunction):
    """``zone_for_temperature(temp, unit)``: the half zone for a °F or °C temperature."""

    class Meta:
        """Function metadata surfaced to DuckDB, agents and vgi-lint."""

        name = "zone_for_temperature"
        description = (
            "USDA hardiness half zone for a minimum temperature in the given unit ('F' or 'C'); "
            "NULL outside the published zones. " + DISCLAIMER
        )
        tags = object_tags(
            title="Zone for Temperature (°F or °C)",
            category="zones",
            keywords="hardiness zone, celsius, temperature to zone, minimum temperature, half zone, unit",
            doc_llm=(
                "Two-argument overload of `zone_for_temperature`: `unit` is a constant, `'F'` or `'C'`. "
                "A Celsius value is converted to °F first, so `zone_for_temperature(-17.8, 'C')` is `'7a'` "
                "(about 0 °F) and `zone_for_temperature(-20, 'C')` is `'6b'` (-4 °F). Values outside "
                "-60..70 °F (about -51.1..21.1 °C) return NULL. " + DISCLAIMER
            ),
            doc_md=(
                "## zone_for_temperature(temp, unit)\n\n"
                "The half zone for a temperature in `'F'` or `'C'`: `zone_for_temperature(-20, 'C')` is "
                "`'6b'`.\n\n"
                f"> {DISCLAIMER}"
            ),
        )
        examples = [
            FunctionExample(
                sql="SELECT c AS temp_c, us_hardiness.main.zone_for_temperature(c, 'C') AS half_zone "
                "FROM (VALUES (-40.0), (-20.0), (-5.0)) v(c) ORDER BY c",
                description="Half zones for winter lows given in Celsius",
            ),
        ]

    @classmethod
    def compute(
        cls,
        temp: Annotated[pa.DoubleArray, Param(doc=_TEMP_DOC)],
        unit: Annotated[str, ConstParam(_UNIT_DOC, choices=["F", "C"])],
    ) -> Annotated[pa.StringArray, Returns()]:
        """Return the half zone of each temperature, converting °C to °F first."""
        if unit == "C":
            temp = pc.add(pc.divide(pc.multiply(temp, 9.0), 5.0), 32.0)
        return zones_for_temperatures(temp)


ZONES_SCHEMA = pa.schema(
    [
        column("half_zone", pa.string(), "Half zone name, '1a' (coldest) to '13b' (warmest).", nullable=False),
        column("zone", pa.int32(), "Whole zone number, 1 to 13 (each spans 10 °F).", nullable=False),
        column("subzone", pa.string(), "'a' for the colder half of the zone, 'b' for the warmer half.", nullable=False),
        column("zone_index", pa.int32(), "Ordinal 0 ('1a') to 25 ('13b'); compare zones with it.", nullable=False),
        column(
            "min_temp_f_low",
            pa.int32(),
            "Lower bound (inclusive) of the mean annual extreme minimum temperature, °F.",
            nullable=False,
        ),
        column("min_temp_f_high", pa.int32(), "Upper bound (exclusive) of the band, °F.", nullable=False),
        column(
            "min_temp_c_low", pa.float64(), "Lower bound of the band in °C (converted, 2 decimals).", nullable=False
        ),
        column(
            "min_temp_c_high", pa.float64(), "Upper bound of the band in °C (converted, 2 decimals).", nullable=False
        ),
    ]
)


@dataclass(slots=True, frozen=True, kw_only=True)
class NoArgs:
    """The table scan takes no arguments."""


@init_single_worker
@bind_fixed_schema
class Zones(TableFunctionGenerator[NoArgs, None]):
    """The 26 half zones of the 2023 map with their temperature bands."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = ZONES_SCHEMA

    class Meta:
        """Function metadata surfaced to DuckDB, agents and vgi-lint."""

        name = "zones"
        description = (
            "The 26 USDA hardiness half zones (1a..13b) with their °F and °C minimum-temperature bands. " + DISCLAIMER
        )
        tags = {
            **object_tags(
                title="Hardiness Zones",
                category="zones",
                keywords="hardiness zones, zone list, temperature bands, half zones, usda zones, zone legend",
                doc_llm=(
                    "Reference table of the 26 half zones of the 2023 USDA Plant Hardiness Zone Map, one row "
                    "per half zone from `'1a'` (-60 to -55 °F) to `'13b'` (65 to 70 °F), with `zone`, `subzone`, "
                    "`zone_index` (0..25, for ordering and comparisons), the °F band (`min_temp_f_low` "
                    "inclusive, `min_temp_f_high` exclusive) and the same band in °C. Join it on `half_zone` "
                    "to the output of `zone_for_zip`, `zone_for_point` or `plants.plant_min_zone_derived`. "
                    + DISCLAIMER
                ),
                doc_md=(
                    "## zones\n\nThe legend of the 2023 USDA Plant Hardiness Zone Map: 26 half zones, "
                    "5 °F each, in °F and °C.\n\n"
                    f"> {DISCLAIMER}"
                ),
            ),
            "vgi.result_columns_schema": result_columns(ZONES_SCHEMA),
            "vgi.executable_examples": json.dumps(
                [
                    {
                        "description": "The two halves of zone 7",
                        "sql": "SELECT half_zone, min_temp_f_low, min_temp_f_high FROM us_hardiness.main.zones "
                        "WHERE zone = 7 ORDER BY zone_index",
                        "expected_result": [["7a", 0, 5], ["7b", 5, 10]],
                    }
                ]
            ),
        }
        examples = [
            FunctionExample(
                sql="SELECT half_zone, min_temp_c_low, min_temp_c_high FROM us_hardiness.main.zones "
                "WHERE zone BETWEEN 5 AND 6 ORDER BY zone_index",
                description="Celsius bands of zones 5 and 6",
            ),
        ]

    @classmethod
    def process(cls, params: ProcessParams[NoArgs], state: None, out: OutputCollector) -> None:
        """Emit all 26 rows."""
        lows = [half_zone_low_f(i) for i in range(HALF_ZONE_COUNT)]
        out.emit(
            pa.RecordBatch.from_pydict(
                {
                    "half_zone": HALF_ZONES,
                    "zone": [i // 2 + 1 for i in range(HALF_ZONE_COUNT)],
                    "subzone": ["ab"[i % 2] for i in range(HALF_ZONE_COUNT)],
                    "zone_index": list(range(HALF_ZONE_COUNT)),
                    "min_temp_f_low": lows,
                    "min_temp_f_high": [lo + 5 for lo in lows],
                    "min_temp_c_low": [f_to_c(lo) for lo in lows],
                    "min_temp_c_high": [f_to_c(lo + 5) for lo in lows],
                },
                schema=params.output_schema,
            )
        )
        out.finish()
