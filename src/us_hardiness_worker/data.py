"""Load the bundled Parquet files (built by ``scripts/build_data.py``) once per process."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from vgi_rpc import ArrowSerializableDataclass

DATA_DIR = Path(__file__).resolve().parent / "data"
BATCH_ROWS = 16_384


@cache
def bundled(name: str) -> pa.Table:
    """Read ``data/<name>.parquet`` (cached for the life of the process)."""
    return pq.read_table(DATA_DIR / f"{name}.parquet")


@dataclass(kw_only=True)
class OffsetCursor(ArrowSerializableDataclass):
    """Externalized scan cursor: rows emitted so far (survives HTTP, where each batch may hit another process)."""

    offset: int = 0


def slice_batch(table: pa.Table, offset: int, schema: pa.Schema, rows: int = BATCH_ROWS) -> pa.RecordBatch:
    """Rows ``offset .. offset+rows`` of ``table`` as one batch typed exactly as ``schema``."""
    part = table.slice(offset, rows)
    arrays = [part.column(f.name).combine_chunks().cast(f.type) for f in schema]
    return pa.RecordBatch.from_arrays(arrays, schema=schema)
