# Changelog

## 0.1.0 (unreleased)

First release.

- Zones: `zone_for_zip`, `zone_for_point`, `zone_for_temperature` (°F, and an overload taking
  `'F'`/`'C'`), and the `zones` and `zip_zones` tables. These are built from the 2023 USDA
  PHZM GIS datasets (PRISM, Oregon State University). The ZIP listing is bundled. The
  temperature grids are downloaded to a cache on first use and checked against their
  SHA-256.
- Plants: the `plants` and `plant_characteristics` tables, built from the USDA NRCS PLANTS
  checklist and characteristics service, and `plants_for_zone(zone, state, native_only)`.
  These use `plant_min_zone_derived`, which this worker derives from the PLANTS minimum
  temperature.
- `grids` table and the `cache_dir` and `grid_url` ATTACH options.

Targets: vgi-python 0.51 · VGI protocol 3.3 · Haybarn 2.0.0rc11 · vgi-lint-check 2.x (unpinned)
