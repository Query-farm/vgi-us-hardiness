<p align="center">
  <img src="https://query.farm/media-kit/products/vgi/vgi-logo.png" alt="Vector Gateway Interface (VGI)" width="320">
</p>

<p align="center"><em>A Query.Farm VGI worker for DuckDB.</em></p>

# Look Up USDA Plant Hardiness Zones and Hardy Plants in DuckDB

> **vgi-us-hardiness** · a [Query.Farm](https://query.farm) VGI worker

[![CI](https://github.com/Query-farm/vgi-us-hardiness/actions/workflows/ci.yml/badge.svg)](https://github.com/Query-farm/vgi-us-hardiness/actions/workflows/ci.yml)
[![vgi worker: query.farm](https://query.farm/media-kit/shields/vgi-worker.svg)](https://query.farm)

> [!IMPORTANT]
> **This is not the official USDA Plant Hardiness Zone Map.** Zone values are derived
> from the 2023 USDA Plant Hardiness Zone Map GIS datasets produced by the PRISM Group at
> Oregon State University. For the official map, go to
> [planthardiness.ars.usda.gov](https://planthardiness.ars.usda.gov).
>
> **Plant zones are derived by this worker.** `plant_min_zone_derived` is computed from the
> USDA PLANTS "Temperature, Minimum (°F)" characteristic. Neither USDA PLANTS nor the USDA
> hardiness map publishes a zone for a plant.

What hardiness zone is a ZIP code or a point in, and which plants survive its winters? This
worker answers both questions in SQL using two public sources:

- **Zones**: the 2023 USDA Plant Hardiness Zone Map (PHZM) GIS datasets from
  [PRISM / Oregon State University](https://prism.oregonstate.edu/phzm/). The ZIP-code listing
  (about 40,500 ZIPs in the contiguous US, Alaska, Hawaii and Puerto Rico) is bundled. The
  1991-2020 mean annual extreme minimum temperature grids (800 m cells for the contiguous US
  and Alaska, 400 m for Hawaii and Puerto Rico) are downloaded into a cache the first time
  you look up a point in that region.
- **Plants**: the [USDA NRCS PLANTS Database](https://plants.usda.gov). The complete
  checklist of 93,144 accepted names and synonyms is bundled, along with the characteristics
  that PLANTS publishes for 2,186 accepted taxa.

```sql
ATTACH 'us_hardiness' (TYPE vgi, LOCATION 'uvx --from git+https://github.com/Query-farm/vgi-us-hardiness@v0.1.0 vgi-us-hardiness-worker');

SELECT us_hardiness.main.zone_for_zip('10001').half_zone;                 -- 7b
SELECT us_hardiness.main.zone_for_point(45.5152, -122.6784);              -- {half_zone: 9a, zone: 9, min_temp_f: 22.0, ...}
SELECT us_hardiness.main.zone_for_temperature(-20.0, 'C');                -- 6b

-- Native shrubs for a Minneapolis ZIP code's zone
SELECT scientific_name, common_name, mature_height_ft_high
FROM us_hardiness.main.plants_for_zone(us_hardiness.main.zone_for_zip('55401').half_zone,
                                       state := 'MN', native_only := true)
WHERE list_contains(growth_habit, 'Shrub')
ORDER BY scientific_name;
```

## Functions and tables

All objects live in `us_hardiness.main`.

| Object | Kind | What it returns |
|---|---|---|
| `zone_for_zip(zip)` | scalar | `STRUCT(zone INTEGER, half_zone VARCHAR, min_temp_f_low INTEGER, min_temp_f_high INTEGER, region VARCHAR)`. Returns NULL for an unknown ZIP. Accepts ZIP+4 and 3-4 digit ZIPs that lost their leading zeros. |
| `zone_for_point(lat, lon)` | scalar | `STRUCT(half_zone, zone, min_temp_f DOUBLE, min_temp_c DOUBLE, region)` from the PRISM grid. Returns NULL off the grids or over water. |
| `zone_for_temperature(temp_f)` | scalar | The half zone (`'1a'` to `'13b'`) for a temperature in °F. Returns NULL outside -60 to 70 °F. |
| `zone_for_temperature(temp, unit)` | scalar | The same, with `unit` set to `'F'` or `'C'`. |
| `plants_for_zone(zone, state := NULL, native_only := false)` | table function | Accepted plants whose derived minimum zone is the given zone or colder. You can limit results to a state (USPS code) and to plants native to that state's PLANTS region. |
| `zones` | table | The 26 half zones, with °F and °C bands and a `zone_index` ordinal. |
| `zip_zones` | table | The full ZIP-code listing: `zip`, `region`, `zone`, `half_zone`, and the °F band. |
| `plants` | table | The PLANTS checklist (PK `symbol`), with characteristics columns for the taxa that have them. |
| `plant_characteristics` | table | Every PLANTS characteristic in long format: `symbol`, `category`, `characteristic`, `value`. |
| `grids` | table | One row per grid region, with its download URL, cache path, cached status and size. |

**Zones.** The 2023 map has 13 zones of 10 °F each. Each zone is split into half zones `a`
(colder) and `b` (warmer) of 5 °F. Zone 1a starts at -60 °F and 13b ends at 70 °F. Each
band includes its lower bound, so 0 °F is 7a and -0.5 °F is 6b.

**Point versus ZIP.** `zone_for_point` reads one grid cell, which gives sub-zonal detail.
`zone_for_zip` returns the official listing for the ZIP's area. The two can differ: for
example, downtown Anchorage's cell is 5b while ZIP 99501 is listed as 5a.

**Plants and zones.** PLANTS publishes numeric characteristics as ranges in its bulk
service. For example, red maple's "Temperature, Minimum (°F)" is `-47 - -43`. The `plants`
table keeps both ends of each range as `min_temp_f_low` and `min_temp_f_high`. The derived
zone is `zone_for_temperature(min_temp_f_high)`, the warm end of the range. That is the
coldest temperature the plant is certainly documented to tolerate. Plants hardier than
-60 °F are clamped to 1a. A plant appears in `plants_for_zone(z)` when its derived zone is
`z` or colder. PLANTS has no heat tolerance or maximum zone, so the results for warm zones
include cold-climate plants.

**Coverage.** Every checklist row has a symbol, scientific name, common name and family.
Habit, duration, nativity, states and characteristics exist only for the 2,186 taxa where
PLANTS publishes characteristics, and 2,099 of those have a minimum temperature. For all
other rows those columns are NULL. PLANTS has per-plant profiles for the rest, but no bulk
download of them (see [DATA-SOURCES.md](DATA-SOURCES.md)).

**Join keys.** ZIP codes are 5-character `VARCHAR`, states are USPS codes and plants use
PLANTS symbols, so the results join with other Query.Farm workers.

**Pushdown.** There is none, and none is needed. The zone tables and the plant data are
bundled and scanned from memory, and `plants_for_zone` filters in memory. Point lookups read
single cells from a memory-mapped grid.

## ATTACH options

| Option | Default | Meaning |
|---|---|---|
| `cache_dir` | per-user cache (`~/Library/Caches/vgi-us-hardiness` on macOS, `$XDG_CACHE_HOME/vgi-us-hardiness` or `~/.cache/vgi-us-hardiness` on Linux, `%LOCALAPPDATA%\vgi-us-hardiness` on Windows) | Where downloaded grids are stored. |
| `grid_url` | `https://prism.oregonstate.edu/phzm/data/2023/` | Where `phzm_<region>_grid_2023.zip` files are fetched from. This can be an http(s) base URL or a local directory, for example a mirror or an offline copy. |

```sql
ATTACH 'us_hardiness' (TYPE vgi, LOCATION '...', cache_dir '/data/phzm-cache');
SELECT region, cached, size_bytes FROM us_hardiness.main.grids ORDER BY region;
```

Grids are downloaded only when `zone_for_point` first needs a region, never at import or
ATTACH. The downloads are about 41 MB for the contiguous US (87 MB unpacked), 15 MB for
Alaska (70 MB unpacked) and under 1 MB each for Hawaii and Puerto Rico. Each file from the
default source is checked against its published SHA-256, unpacked atomically and reused
after that. Files from any other `grid_url` get their own cache subdirectory and a
structural check, because their checksums are unknown. To free disk space, delete the
cache; grids are fetched again on demand.

## Quick start

**Prerequisites:** Python 3.13+ and [uv](https://docs.astral.sh/uv/).

Run it straight from a release tag:

```sql
SET allow_extension_repositories = 'allowed';
CREATE EXTENSION REPOSITORY IF NOT EXISTS query_farm WITH PREFIX 'https://duckdb-extensions.query.farm';
INSTALL vgi FROM query_farm;
LOAD vgi FROM query_farm;
ATTACH 'us_hardiness' (TYPE vgi, LOCATION 'uvx --from git+https://github.com/Query-farm/vgi-us-hardiness@v0.1.0 vgi-us-hardiness-worker');
```

Or build it from a clone:

```bash
git clone https://github.com/Query-farm/vgi-us-hardiness && cd vgi-us-hardiness
make build
```

```sql
ATTACH 'us_hardiness' (TYPE vgi, LOCATION '/abs/path/to/vgi-us-hardiness/.venv/bin/vgi-us-hardiness-worker');
```

On **Haybarn 2.0** (`uvx haybarn-cli`), run `INSTALL vgi FROM community; LOAD vgi;` and then
use the same `ATTACH`. The worker is not published to PyPI.

## Development

```text
src/us_hardiness_worker/
  zones.py       # zone arithmetic, zones table, zone_for_temperature (°F and °F/°C overloads)
  zip_zones.py   # zone_for_zip, zip_zones table (bundled PHZM ZIP listing)
  grid.py        # zone_for_point, grids: PRISM BIL grids, download + verify + mmap
  plants.py      # plants, plant_characteristics tables; plants_for_zone
  data.py        # bundled Parquet loader, externalized scan cursor
  meta.py        # catalog metadata, the disclaimer text
  worker.py      # catalog assembly, ATTACH options, entry point
  data/          # zip_zones / plants / plant_characteristics .parquet (built by scripts/build_data.py)
scripts/build_data.py          # rebuild the bundle from the upstream sources (cached in .cache/raw)
scripts/make_grid_fixtures.py  # rebuild test/fixtures/grids from the official grid zips
test/fixtures/grids/           # real HI/PR grids plus 1-degree CONUS and 0.5-degree Alaska crops
tests/                         # pytest
test/sql/                      # SQLLogicTest suite (stdio, launch and http transports)
```

```bash
make test-unit      # pytest
make test-sql       # SQL suite over stdio (TRANSPORT=launch|http for the others)
make lint-explain   # metadata quality (vgi-lint, gated at info)
make check          # everything CI runs
uv run python scripts/build_data.py   # refresh the bundled data (polite: 1 request/s)
```

The tests run offline. The SQL suite attaches with `grid_url` pointing at
`test/fixtures/grids` and with `cache_dir` set to a scratch directory. The worker's only
runtime dependency is `vgi-python`, and the grid reader is pure Python (`mmap` plus
`struct`). `stdout` carries the Arrow protocol, so logs go to stderr.

## Data terms

The code license (below) does not cover the data. See [DATA-SOURCES.md](DATA-SOURCES.md)
for sources, retrieval dates, checksums and the exact terms.

- **2023 USDA PHZM GIS datasets** (ZIP listing, bundled; temperature grids, downloaded at
  runtime). Oregon State University owns these under a cooperative agreement with
  USDA-ARS. They may be freely reproduced and redistributed. If the data is altered, it
  must carry an explicit and prominently displayed disclaimer that it is not the official
  USDA Plant Hardiness Zone Map, and the USDA-ARS and OSU logos must not be used. This
  worker reformats and derives from the data, so it carries that disclaimer here, in the
  catalog description and in every zone function's description. It does not use either
  logo.
- **USDA NRCS PLANTS Database** (checklist and characteristics, bundled). This is a US
  federal government work and in the public domain. Suggested citation: USDA, NRCS. 2026.
  The PLANTS Database (https://plants.usda.gov, 15 November 2026). National Plant Data
  Team, Greensboro, NC USA.

## Authorship & License

Written by [Query.Farm](https://query.farm). Released under the BSD Zero Clause License
(see [LICENSE](LICENSE)).

Copyright 2026 Query Farm LLC - https://query.farm
