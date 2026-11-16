# Data sources

All files were retrieved on **2026-11-15**. Raw downloads are cached in `.cache/raw/`, which
is gitignored. `scripts/build_data.py` turns them into the bundled Parquet files in
`src/us_hardiness_worker/data/`.

> **Not the official USDA Plant Hardiness Zone Map.** The zone values in this worker are
> derived from the 2023 USDA PHZM GIS datasets (PRISM Group, Oregon State University). For
> the official map, go to https://planthardiness.ars.usda.gov.

## 1. 2023 USDA Plant Hardiness Zone Map GIS datasets (PRISM Group, Oregon State University)

- Page: https://prism.oregonstate.edu/phzm/
- Producer: PRISM Group, Oregon State University. Released by USDA-ARS in 2023. Based on
  the 1991-2020 mean annual extreme minimum temperature.
- Terms of use, quoted verbatim from the page and from `phzm_terms_of_use.txt` in every grid zip:

  > Under the terms of a Specific Cooperative Agreement between the US Department of
  > Agriculture and Oregon State University (OSU), OSU retains ownership of the GIS datasets
  > underlying the official 2023 and 2012 USDA Plant Hardiness Zone Maps. These datasets may
  > be freely reproduced and redistributed, subject to the following conditions: (1) Both the
  > USDA-ARS logo and OSU logo must be prominently displayed on any maps derived from the GIS
  > datasets. (2) The data may not be altered in any way unless (a) there is an explicit and
  > prominently displayed disclaimer that the map is not the official USDA Plant Hardiness
  > Zone Map, and (b) the USDA-ARS and OSU logos are eliminated.

  The page also says: "These files are available free of charge on this site. However, they
  continue to be the property of Oregon State University and are subject to OSU's terms of
  use."

- How this worker complies: the data is redistributed, which is permitted. It is reformatted
  and derived (ZIP listing to Parquet, grid cells to zones, temperature arithmetic), so the
  disclaimer appears in the README, the catalog and schema descriptions, and the description
  of every zone function and table. The worker draws no maps and uses neither logo.

### Bundled: ZIP-code listings

These four files are combined into `zip_zones.parquet` (40,502 rows). The build checks
that each listed zone matches its listed temperature range.

| File | URL | SHA-256 |
|---|---|---|
| `phzm_us_zipcode_2023.csv` | https://prism.oregonstate.edu/phzm/data/2023/phzm_us_zipcode_2023.csv | `bd5a10ad97d6442784421a488e0ddfb775c44ee64b0b4166f6508495c3a1283c` |
| `phzm_ak_zipcode_2023.csv` | https://prism.oregonstate.edu/phzm/data/2023/phzm_ak_zipcode_2023.csv | `726bf7821c7ff73e390f148e0f68a431f2d9b3b95dcaff417c567c8090f39c70` |
| `phzm_hi_zipcode_2023.csv` | https://prism.oregonstate.edu/phzm/data/2023/phzm_hi_zipcode_2023.csv | `c8f5d24bb83e126691673f2a15da24aaf575b02549fa3e0ab40fb5111bcc971b` |
| `phzm_pr_zipcode_2023.csv` | https://prism.oregonstate.edu/phzm/data/2023/phzm_pr_zipcode_2023.csv | `ab71e6c925acbe30e4baea43de59dfc7cb42f3665c1458a4c61f0ecd646418b4` |

### Fetched at runtime: temperature grids

These are float32 ESRI BIL grids in °F, NAD83 geographic coordinates. The resolution is
30 arc-seconds (about 800 m) for CONUS and Alaska and 15 arc-seconds (about 400 m) for
Hawaii and Puerto Rico. They are not committed. `zone_for_point` downloads a region's zip
on first use and checks it against the SHA-256 in `grid.py`.

| File | Size | SHA-256 |
|---|---|---|
| `phzm_us_grid_2023.zip` | 41,068,950 B | `7834bb8f93e5b62c1e09374ba65afa7c91d638a0456ea241f668d7023cc4b785` |
| `phzm_ak_grid_2023.zip` | 14,886,757 B | `9a599d3a360991ebc94a5c6561a82570a1def068f873bd5e0964282fb2f7f2b6` |
| `phzm_hi_grid_2023.zip` | 332,869 B | `0e88f66779923e79a9ca83531db3ea36213f933f85ba5d68b150ddad661819da` |
| `phzm_pr_grid_2023.zip` | 156,554 B | `c86e41d7e0a4d94fd5081f852980fae8c42e0bc55b30e5e046703cdfe67c07c3` |

Base URL: https://prism.oregonstate.edu/phzm/data/2023/ (Last-Modified 2023-11-15).

### Test fixtures: `test/fixtures/grids/`

The Hawaii and Puerto Rico zips are copied unchanged. The CONUS zip is cropped to
45-46°N, 123.5-122°W (Portland, Oregon) and the Alaska zip to 60.9-61.4°N, 150.2-149.4°W
(Anchorage). `scripts/make_grid_fixtures.py` makes the crops, and each crop keeps the
original terms-of-use file. Redistribution is permitted. The crops are altered data and
carry the same disclaimer through this file and the README.

## 2. USDA NRCS PLANTS Database

- Site: https://plants.usda.gov (USDA Natural Resources Conservation Service, National
  Plant Data Team)
- License: a work of the US federal government, so it is in the public domain in the US
  (17 U.S.C. § 105). The PLANTS image library carries separate copyrights. This worker uses
  no images.
- Suggested citation: USDA, NRCS. 2026. The PLANTS Database (https://plants.usda.gov,
  15 November 2026). National Plant Data Team, Greensboro, NC USA.

### Bundled (`plants.parquet`, `plant_characteristics.parquet`)

| Source | URL | SHA-256 |
|---|---|---|
| Complete PLANTS checklist | https://plants.sc.egov.usda.gov/DocumentLibrary/Txt/plantlst.txt | `2a08310dfc2fd33dd441e935ed169ab680bcea367e7bb5fb6c11d450d0c0d0bf` |
| Taxa with characteristics | https://plantsservices.sc.egov.usda.gov/api/characteristicSearchResults | `f06e01dc0280f9baaf5735d8fe508942ee8f45ff448f54006bd5bfb0af27e99a` |
| Filters: Filtering (duration, group, growth habit, nativity, state) | `.../api/characteristicSearchFilterResults?type=Filtering` | `8fe634e34a4b4105cd4af6e286b7afe5956d29b667134a0044bd9e779c9a4b28` |
| Filters: Growth Requirements | `...?type=Growth%20Requirements` | `4729d132478763d2e8a88426570ea42ecf4833f4447639b5d7ba094e7cf3e4d9` |
| Filters: Morphology | `...?type=Morphology` | `a59359205d61baa6b53bb82a1fd1a05423fc3f6dedcecb9d5e30d825819f4e94` |
| Filters: Reproduction | `...?type=Reproduction` | `5f1126333fdf36b4285054aeebefdc312921de9452ea03f1c14cf6bcfa85010d` |
| Filters: Suitability Use | `...?type=Suitability%20Use` | `7e12e39cac700a8b0f05c60ee7dd5cef2a692a2a667d652285369e3e4c79429b` |

Bundle checksums (as built 2026-11-15):

| File | SHA-256 |
|---|---|
| `zip_zones.parquet` | `0622fc640c5021cb391beabb883c3a4d29371c2d1d027ced18d63868051ea529` |
| `plants.parquet` | `6158273e279ea8414b57e2dd5a46356aa25734324c9cb0ff7f655711de626e40` |
| `plant_characteristics.parquet` | `c1ddd7d458307356c2c4f57cd8bbaee3c68c31b77e622269d453c9c05740542f` |

### How the PLANTS data was found, and its limits

PLANTS has no documented bulk API. The web app at plants.usda.gov is an Angular single-page
application backed by `https://plantsservices.sc.egov.usda.gov/api/`. Reading its bundle
showed which endpoints it calls:

- `DocumentLibrary/Txt/plantlst.txt` is the "complete PLANTS checklist". It has five
  columns: Symbol, Synonym Symbol, Scientific Name with Author, Common Name and Family. It
  lists 93,144 symbols, of which 48,990 are accepted names.
- `characteristicSearchResults` lists the 2,186 accepted taxa that have characteristics,
  with their numeric ids.
- `characteristicSearchFilterResults?type=...` powers the characteristics search filters.
  For every characteristic value it lists the ids of the plants that have it. Inverting
  that index gives every characteristic of every plant in five requests.

Limits:

1. **Numeric characteristics are ranges.** The filter service buckets numeric values. For
   example, "Temperature, Minimum (°F)" appears as `-47 - -43` for red maple, whose profile
   says -47. Exact values exist only in the per-plant `PlantCharacteristics/{id}` endpoint,
   one request per plant (2,186 requests). That is beyond this build's request budget, so
   the worker stores the bucket bounds (`min_temp_f_low` and `min_temp_f_high`) and derives
   the zone from the warm end. Two upstream buckets overlap at 13 °F (`8 - 13` and
   `13 - 17`), and the `N/A` bucket shares an id with `-2 - 2`. The worker parses bounds
   from the labels and ignores `N/A`.
2. **Habit, duration, nativity and states cover only the 2,186 characteristic taxa.** The
   checklist has no such columns. They are available per plant from
   `PlantProfile?symbol=...`, or per state from `plants-search-results`. A state-search
   request for Rhode Island timed out on the server (HTTP 500 after 30 s) during
   exploration. These columns are NULL for the other ~91,000 checklist rows.
3. **82 plant ids** in the filter index are not among the accepted characteristic taxa;
   they are probably synonyms or cultivars. They are dropped.
4. `states` lists USPS codes of US states, DC and territories. Canadian provinces in the
   PLANTS distribution are omitted. Navassa Island is mapped to `UM`.
