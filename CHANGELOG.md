# Changelog

## 0.2.0 (2026-11-25)

- Speaks VGI protocol 3.4: require vgi-python `>=0.55.2,<0.56`; uv.lock re-locked. This release
  needs Haybarn 2.0.1 (or stock DuckDB 2.0 with `INSTALL vgi FROM query_farm`): Haybarn 2.0.0
  loads a 3.3 `vgi` extension that cannot attach it.
- SQL tests run on haybarn-unittest 2.0.1rc3.
- `serve` extra is `vgi-python[http,oauth]`; the Docker image installs it, so the image runs in
  Orchard hosted mode (`VGI_ORCHARD_HOSTED=1` with VGI_JWT_ISSUER, VGI_JWT_AUDIENCE,
  VGI_JWT_JWKS_URI and VGI_SIGNING_KEY). The HEALTHCHECK uses `HEAD /health`, which stays
  anonymous in hosted mode.

## 0.1.1 (2026-11-16)

- Bump vgi-python to 0.52.2.
- Requires Python 3.14 (CI, Docker image and tooling moved to 3.14).

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
