"""Point lookups on the PRISM 2023 PHZM temperature grids: ``zone_for_point`` and ``grids``.

The grids (1991-2020 mean annual extreme minimum temperature, °F, float32 ESRI BIL in
NAD83 geographic coordinates) are too large to bundle, so each region's zip is
downloaded on the first lookup of a point in that region, checked against its
published SHA-256, unpacked into the cache directory and memory-mapped. Nothing
is downloaded at import or ATTACH time.

ATTACH options:

* ``cache_dir``: where grids are stored (default: the per-user cache directory).
* ``grid_url``: where the grid zips are fetched from: an http(s) base URL or a
  local directory holding ``phzm_<region>_grid_2023.zip`` files. A non-default
  source gets its own cache subdirectory and is validated structurally, since
  its checksums are unknown.
"""

from __future__ import annotations

import hashlib
import json
import logging
import mmap
import os
import shutil
import ssl
import struct
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, ClassVar

import pyarrow as pa
import truststore
from vgi.arguments import Param, Returns
from vgi.metadata import FunctionExample
from vgi.scalar_function import BindParameters, BindResult, ScalarFunction
from vgi.table_function import (
    BindParams,
    OutputCollector,
    ProcessParams,
    TableCardinality,
    TableFunctionGenerator,
    bind_fixed_schema,
    init_single_worker,
)
from vgi_rpc import ArrowSerializableDataclass

from .meta import DISCLAIMER, PHZM_URL, column, object_tags, result_columns
from .zones import f_to_c, half_zone_index, half_zone_name

log = logging.getLogger(__name__)

DEFAULT_GRID_URL = "https://prism.oregonstate.edu/phzm/data/2023/"
USER_AGENT = "vgi-us-hardiness/0.1 (+https://query.farm)"
NODATA = -9999.0


@dataclass(frozen=True, slots=True)
class RegionSpec:
    """One published grid: its file, checksum and (official) extent, used to route points."""

    code: str
    name: str
    sha256: str
    nrows: int
    ncols: int
    ulx: float  # longitude of the centre of the upper-left cell
    uly: float  # latitude of the centre of the upper-left cell
    dim: float  # cell size in degrees

    @property
    def zip_name(self) -> str:
        """File name of the published zip."""
        return f"phzm_{self.code}_grid_2023.zip"

    def contains(self, lat: float, lon: float) -> bool:
        """Whether the official grid extent covers the point (longitude already normalized)."""
        half = self.dim / 2
        return (
            self.ulx - half <= lon < self.ulx + (self.ncols - 0.5) * self.dim
            and self.uly - (self.nrows - 0.5) * self.dim < lat <= self.uly + half
        )


# Header values and SHA-256 of the files published at DEFAULT_GRID_URL (retrieved 2026-11-15).
REGIONS: tuple[RegionSpec, ...] = (
    RegionSpec(
        "hi", "hawaii", "0e88f66779923e79a9ca83531db3ea36213f933f85ba5d68b150ddad661819da",
        811, 1328, -160.2916666667, 22.25, 0.0041666667,
    ),
    RegionSpec(
        "pr", "puerto_rico", "c86e41d7e0a4d94fd5081f852980fae8c42e0bc55b30e5e046703cdfe67c07c3",
        164, 557, -67.3166666667, 18.55, 0.0041666667,
    ),
    RegionSpec(
        "ak", "alaska", "9a599d3a360991ebc94a5c6561a82570a1def068f873bd5e0964282fb2f7f2b6",
        2491, 7043, -187.65, 71.75, 0.008333333333,
    ),
    RegionSpec(
        "us", "conus", "7834bb8f93e5b62c1e09374ba65afa7c91d638a0456ea241f668d7023cc4b785",
        3105, 7025, -125.016666666666, 49.933333333333, 0.008333333333,
    ),
)  # fmt: skip
REGION_BY_CODE = {r.code: r for r in REGIONS}


def normalize_lon(region: RegionSpec, lon: float) -> float:
    """Alaska's grid runs west past the antimeridian to -187.65°; map eastern longitudes onto it."""
    return lon - 360.0 if region.code == "ak" and lon > 0 else lon


def route(lat: float, lon: float) -> RegionSpec | None:
    """Return the region whose grid covers a point, if any."""
    for region in REGIONS:
        if region.contains(lat, normalize_lon(region, lon)):
            return region
    return None


@dataclass(kw_only=True)
class GridConfig(ArrowSerializableDataclass):
    """The grid settings of one call, from its ATTACH options."""

    cache_dir: str = ""
    grid_url: str = ""


def config_from_options(options: dict[str, Any]) -> GridConfig:
    """Build the grid config from ATTACH options (missing options use the defaults)."""
    return GridConfig(cache_dir=str(options.get("cache_dir") or ""), grid_url=str(options.get("grid_url") or ""))


def options_of(params: Any) -> dict[str, Any]:
    """ATTACH options of a call, or ``{}`` when it was not made through an ATTACHed catalog."""
    try:
        return dict(params.attach_options or {})
    except Exception:  # not invoked through an ATTACHed catalog
        return {}


def default_cache_root() -> Path:
    """Per-user cache directory (macOS ~/Library/Caches, Windows %LOCALAPPDATA%, else $XDG_CACHE_HOME)."""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "vgi-us-hardiness"


def source_url(cfg: GridConfig) -> str:
    """Return the base location the grid zips come from."""
    url = cfg.grid_url or DEFAULT_GRID_URL
    return url if url.endswith(("/", "\\")) else url + "/"


def is_default_source(cfg: GridConfig) -> bool:
    """Whether grids come from the official PRISM location (and so have known checksums)."""
    return source_url(cfg) == DEFAULT_GRID_URL


def cache_path(cfg: GridConfig) -> Path:
    """Directory holding this source's unpacked grids."""
    root = Path(cfg.cache_dir).expanduser() if cfg.cache_dir else default_cache_root()
    if is_default_source(cfg):
        return root / "phzm-2023"
    return root / ("source-" + hashlib.sha256(source_url(cfg).encode()).hexdigest()[:12])


@dataclass(frozen=True, slots=True)
class Header:
    """The fields of an ESRI BIL ``.hdr`` this reader needs."""

    nrows: int
    ncols: int
    ulx: float
    uly: float
    xdim: float
    ydim: float
    nodata: float


def parse_header(text: str) -> Header:
    """Parse a PRISM BIL header, rejecting layouts this reader cannot read.

    Args:
        text: Contents of the ``.hdr`` file.

    Returns:
        The parsed header.

    Raises:
        ValueError: The header is not a single-band little-endian float32 BIL grid.

    """
    kv: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            kv[parts[0].upper()] = parts[1]
    expected = {"BYTEORDER": "I", "LAYOUT": "BIL", "NBANDS": "1", "NBITS": "32", "PIXELTYPE": "FLOAT"}
    for key, value in expected.items():
        if kv.get(key, "").upper() != value:
            raise ValueError(f"unsupported grid header: {key}={kv.get(key)!r}, expected {value!r}")
    return Header(
        nrows=int(kv["NROWS"]),
        ncols=int(kv["NCOLS"]),
        ulx=float(kv["ULXMAP"]),
        uly=float(kv["ULYMAP"]),
        xdim=float(kv["XDIM"]),
        ydim=float(kv["YDIM"]),
        nodata=float(kv.get("NODATA", NODATA)),
    )


class Grid:
    """A memory-mapped temperature grid."""

    def __init__(self, bil: Path, header: Header) -> None:
        """Map ``bil``; its size must match the header."""
        self.header = header
        expected = header.nrows * header.ncols * 4
        size = bil.stat().st_size
        if size != expected:
            raise ValueError(f"{bil} is {size} bytes, expected {expected} from its header")
        with bil.open("rb") as fh:
            self._mm = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)

    def value(self, lat: float, lon: float) -> float | None:
        """Temperature (°F) of the cell containing the point, or None outside the grid or on no-data cells."""
        h = self.header
        col = int((lon - (h.ulx - h.xdim / 2)) // h.xdim)
        row = int(((h.uly + h.ydim / 2) - lat) // h.ydim)
        if not (0 <= row < h.nrows and 0 <= col < h.ncols):
            return None
        (v,) = struct.unpack_from("<f", self._mm, (row * h.ncols + col) * 4)
        if v != v or v <= h.nodata + 0.5:
            return None
        return float(v)


_GRIDS: dict[tuple[str, str], Grid] = {}
_LOCK = threading.Lock()


def _download(url: str, dest: Path) -> None:
    """Fetch ``url`` (http(s), file:// or a local path) to ``dest``, retrying 429/5xx with backoff.

    Args:
        url: Source location.
        dest: Local file to write.

    Raises:
        RuntimeError: The download failed after retries, or the server answered with an error status.

    """
    if not url.startswith(("http://", "https://")):
        src = Path(url.removeprefix("file://"))
        if not src.is_file():
            raise RuntimeError(f"grid file not found: {src}")
        shutil.copyfile(src, dest)
        return
    # The OS trust store: uv-managed and some packaged Pythons ship no usable CA bundle.
    context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    delay = 2.0
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=120, context=context) as resp, dest.open("wb") as out:
                shutil.copyfileobj(resp, out, 1 << 20)
            return
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 3:
                raise RuntimeError(f"downloading {url} failed: HTTP {exc.code}") from exc
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            time.sleep(float(retry_after) if retry_after and retry_after.isdigit() else delay)
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == 3 or isinstance(getattr(exc, "reason", None), ssl.SSLError):
                raise RuntimeError(f"downloading {url} failed: {exc}") from exc
            time.sleep(delay)
        delay *= 2
    raise RuntimeError(f"downloading {url} failed")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _unpacked(region: RegionSpec, cfg: GridConfig) -> tuple[Path, Path]:
    base = cache_path(cfg)
    stem = f"phzm_{region.code}_grid_2023"
    return base / f"{stem}.bil", base / f"{stem}.hdr"


def fetch_grid(region: RegionSpec, cfg: GridConfig) -> tuple[Path, Path]:
    """Make sure ``region``'s grid is unpacked in the cache, downloading it if needed.

    Args:
        region: The grid region.
        cfg: Cache directory and source location.

    Returns:
        Paths of the cached ``.bil`` and ``.hdr`` files.

    Raises:
        RuntimeError: The download failed, the checksum did not match, or the zip lacks the grid files.

    """
    bil, hdr = _unpacked(region, cfg)
    if bil.exists() and hdr.exists():
        return bil, hdr
    bil.parent.mkdir(parents=True, exist_ok=True)
    url = source_url(cfg) + region.zip_name
    log.info("downloading %s into %s", url, bil.parent)
    with tempfile.TemporaryDirectory(dir=bil.parent, prefix=".download-") as tmp:
        zpath = Path(tmp) / region.zip_name
        _download(url, zpath)
        if is_default_source(cfg):
            got = _sha256(zpath)
            if got != region.sha256:
                raise RuntimeError(f"{url}: SHA-256 {got} does not match the expected {region.sha256}")
        try:
            with zipfile.ZipFile(zpath) as zf:
                names = {Path(n).name: n for n in zf.namelist()}
                for target in (bil, hdr):
                    member = names.get(target.name)
                    if member is None:
                        raise RuntimeError(f"{url} does not contain {target.name}")
                    part = Path(tmp) / target.name
                    with zf.open(member) as src, part.open("wb") as out:
                        shutil.copyfileobj(src, out, 1 << 20)
        except zipfile.BadZipFile as exc:
            raise RuntimeError(f"{url} is not a valid zip file") from exc
        # Validate before publishing: a header we can read and a data file of the right size.
        header = parse_header((Path(tmp) / hdr.name).read_text(encoding="ascii", errors="replace"))
        if (Path(tmp) / bil.name).stat().st_size != header.nrows * header.ncols * 4:
            raise RuntimeError(f"{url}: grid size does not match its header")
        # Publish atomically, data first: a cached grid is one whose header exists.
        os.replace(Path(tmp) / bil.name, bil)
        os.replace(Path(tmp) / hdr.name, hdr)
    return bil, hdr


def load_grid(region: RegionSpec, cfg: GridConfig) -> Grid:
    """Return the memory-mapped grid of ``region``, downloading it on first use."""
    key = (str(cache_path(cfg)), region.code)
    with _LOCK:
        grid = _GRIDS.get(key)
        if grid is None:
            bil, hdr = fetch_grid(region, cfg)
            grid = Grid(bil, parse_header(hdr.read_text(encoding="ascii", errors="replace")))
            _GRIDS[key] = grid
        return grid


POINT_STRUCT = pa.struct(
    [
        pa.field("half_zone", pa.string()),
        pa.field("zone", pa.int32()),
        pa.field("min_temp_f", pa.float64()),
        pa.field("min_temp_c", pa.float64()),
        pa.field("region", pa.string()),
    ]
)


def lookup_points(lats: list[float | None], lons: list[float | None], cfg: GridConfig) -> pa.StructArray:
    """Zone structs for points; NULL off the grids, over water/no-data cells, or for NULL input."""
    rows: list[dict[str, Any] | None] = []
    for lat, lon in zip(lats, lons, strict=True):
        if lat is None or lon is None or lat != lat or lon != lon:
            rows.append(None)
            continue
        region = route(lat, lon)
        if region is None:
            rows.append(None)
            continue
        temp = load_grid(region, cfg).value(lat, normalize_lon(region, lon))
        idx = None if temp is None else half_zone_index(temp)
        if temp is None:
            rows.append(None)
            continue
        rows.append(
            {
                "half_zone": None if idx is None else half_zone_name(idx),
                "zone": None if idx is None else idx // 2 + 1,
                "min_temp_f": round(temp, 2),
                "min_temp_c": f_to_c(temp),
                "region": region.name,
            }
        )
    return pa.array(rows, POINT_STRUCT)  # type: ignore[return-value]


_ACTIVE: ContextVar[GridConfig | None] = ContextVar("us_hardiness_grid_config", default=None)


class ZoneForPoint(ScalarFunction):
    """``zone_for_point(lat, lon)``: zone and temperature at a point from the PRISM grid."""

    class Meta:
        """Function metadata surfaced to DuckDB, agents and vgi-lint."""

        name = "zone_for_point"
        description = (
            "Hardiness zone and 1991-2020 mean annual extreme minimum temperature at a latitude/longitude, "
            "from the PRISM 2023 PHZM grid (downloaded to a cache on first use). " + DISCLAIMER
        )
        tags = object_tags(
            title="Zone for Point",
            category="zones",
            keywords="latitude longitude, coordinates, point lookup, hardiness zone, extreme minimum, prism",
            doc_llm=(
                "Look up the 2023 USDA Plant Hardiness Zone at a point (`lat`, `lon` in decimal degrees, NAD83/WGS84) "
                "by reading the PRISM 1991-2020 mean annual extreme minimum temperature grid (800 m cells for the "
                "contiguous US and Alaska, 400 m for Hawaii and Puerto Rico). Returns `STRUCT`(`half_zone` e.g. '6b', "
                "`zone` `INTEGER`, `min_temp_f` `DOUBLE`, `min_temp_c` `DOUBLE`, `region` 'conus'/'alaska'/'hawaii'/"
                "'puerto_rico'). NULL for NULL input, points outside the four grids (e.g. Canada, open ocean) or "
                "no-data cells such as water. The first lookup in a region downloads that region's grid once into the "
                "cache (about 41 MB contiguous US, 15 MB Alaska, under 1 MB Hawaii/Puerto Rico); later lookups are "
                "local. Prefer `zone_for_zip` when you have a ZIP code. Grid cells give sub-zonal detail, so a point "
                "may differ from its ZIP code's listed zone. " + DISCLAIMER
            ),
            doc_md=(
                "## zone_for_point(lat, lon)\n\n"
                "The hardiness zone and the underlying temperature at a coordinate, read from the PRISM grid "
                f"([source]({PHZM_URL})). The region's grid is downloaded to the cache on first use.\n\n"
                f"> {DISCLAIMER}"
            ),
        )
        examples = [
            FunctionExample(
                sql="SELECT p.half_zone, p.min_temp_f FROM "
                "(SELECT us_hardiness.main.zone_for_point(21.3069, -157.8583) AS p)",
                description="Zone and winter low at Honolulu (downloads the small Hawaii grid on first use)",
            ),
        ]

    @classmethod
    def on_bind(cls, params: BindParameters) -> BindResult:
        """Capture the ATTACH grid settings for the execution phase."""
        return BindResult(output_type=POINT_STRUCT, opaque_data=config_from_options(options_of(params)))

    @classmethod  # type: ignore[misc]  # wraps the SDK's per-batch hook to expose bind settings
    def process(
        cls, *, batch: pa.RecordBatch, init_call: Any, init_response: Any, storage: Any, auth_context: Any
    ) -> pa.RecordBatch:
        """Make the bound grid settings visible to ``compute`` for this batch."""
        raw = getattr(init_call, "bind_opaque_data", None)
        token = _ACTIVE.set(GridConfig.deserialize_from_bytes(raw) if raw else GridConfig())
        try:
            return super().process(
                batch=batch,
                init_call=init_call,
                init_response=init_response,
                storage=storage,
                auth_context=auth_context,
            )
        finally:
            _ACTIVE.reset(token)

    @classmethod
    def compute(
        cls,
        lat: Annotated[pa.DoubleArray, Param(doc="Latitude in decimal degrees (north positive).")],
        lon: Annotated[
            pa.DoubleArray, Param(doc="Longitude in decimal degrees (east positive, so the US is negative).")
        ],
    ) -> Annotated[pa.StructArray, Returns(arrow_type=POINT_STRUCT)]:
        """Return the zone struct for every point."""
        cfg = _ACTIVE.get() or GridConfig()
        return lookup_points(lat.to_pylist(), lon.to_pylist(), cfg)


CACHE_SCHEMA = pa.schema(
    [
        column("region", pa.string(), "Grid region: 'conus', 'alaska', 'hawaii' or 'puerto_rico'.", nullable=False),
        column("source_url", pa.string(), "Where the region's grid zip is downloaded from.", nullable=False),
        column("cache_path", pa.string(), "Local path of the unpacked grid (.bil).", nullable=False),
        column("cached", pa.bool_(), "Whether the grid is already in the cache.", nullable=False),
        column("size_bytes", pa.int64(), "Size of the cached grid data file in bytes; NULL when not cached."),
        column(
            "expected_sha256",
            pa.string(),
            "Published SHA-256 the download is checked against; NULL for a non-default grid_url.",
        ),
    ]
)


@dataclass(slots=True, frozen=True, kw_only=True)
class NoArgs:
    """The ``grids`` table scan takes no arguments."""


@init_single_worker
@bind_fixed_schema
class CacheInfo(TableFunctionGenerator[NoArgs, None]):
    """Where each region's grid lives and whether it is cached."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = CACHE_SCHEMA

    class Meta:
        """Function metadata surfaced to DuckDB, agents and vgi-lint."""

        name = "grids"
        description = "One row per temperature-grid region: download URL, local cache path, cached or not, size"
        tags = {
            **object_tags(
                title="Temperature Grids",
                category="cache",
                keywords="cache, download, grid files, disk usage, cache directory, prism grids",
                doc_llm=(
                    "Report the state of the local cache that `zone_for_point` reads: one row per grid region "
                    "('hawaii', 'puerto_rico', 'alaska', 'conus') with the download `source_url`, the local "
                    "`cache_path`, whether it is `cached`, its `size_bytes` and the `expected_sha256` checked on "
                    "download. Never downloads anything. Reflects the ATTACH options `cache_dir` and `grid_url`. "
                    "Delete the files under the cache path to reclaim space; they are fetched again on demand."
                ),
                doc_md=(
                    "## grids\n\nA table: which PRISM temperature grids are cached locally, where, and how big. "
                    "Set `cache_dir` / `grid_url` at ATTACH to change the location or source."
                ),
            ),
            "vgi.result_columns_schema": result_columns(CACHE_SCHEMA),
            "vgi.executable_examples": json.dumps(
                [
                    {
                        "description": "The four grid regions",
                        "sql": "SELECT region FROM us_hardiness.main.grids ORDER BY region",
                        "expected_result": [["alaska"], ["conus"], ["hawaii"], ["puerto_rico"]],
                    }
                ]
            ),
        }
        examples = [
            FunctionExample(
                sql="SELECT region, cached, size_bytes FROM us_hardiness.main.grids ORDER BY region",
                description="Which grids are already downloaded",
            ),
        ]

    @classmethod
    def cardinality(cls, params: BindParams[NoArgs]) -> TableCardinality:
        """Report one row per region."""
        return TableCardinality(estimate=len(REGIONS), max=len(REGIONS))

    @classmethod
    def process(cls, params: ProcessParams[NoArgs], state: None, out: OutputCollector) -> None:
        """Emit one row per region."""
        cfg = config_from_options(options_of(params))
        rows: dict[str, list[Any]] = {f.name: [] for f in CACHE_SCHEMA}
        default = is_default_source(cfg)
        for region in REGIONS:
            bil, hdr = _unpacked(region, cfg)
            cached = bil.exists() and hdr.exists()
            rows["region"].append(region.name)
            rows["source_url"].append(source_url(cfg) + region.zip_name)
            rows["cache_path"].append(str(bil))
            rows["cached"].append(cached)
            rows["size_bytes"].append(bil.stat().st_size if cached else None)
            rows["expected_sha256"].append(region.sha256 if default else None)
        out.emit(pa.RecordBatch.from_pydict(rows, schema=params.output_schema))
        out.finish()
