"""Point-in-time storage: DuckDB tables, raw payloads, coverage, and run manifests (spec §4.7, §9)."""

from macrosys.store.coverage import CoverageReport, SeriesCoverage, coverage_report
from macrosys.store.manifest import Manifest, TableStat, build_manifest, read_manifest, write_manifest
from macrosys.store.observations import ObservationStore, PointInTime
from macrosys.store.raw import FileRawStore, redact_secrets
from macrosys.store.schema import SCHEMA_VERSION, create_schema
from macrosys.store.state import SnapshotStateView, WorldStateStore

__all__ = [
    "SCHEMA_VERSION",
    "CoverageReport",
    "FileRawStore",
    "Manifest",
    "ObservationStore",
    "PointInTime",
    "SeriesCoverage",
    "SnapshotStateView",
    "TableStat",
    "WorldStateStore",
    "build_manifest",
    "coverage_report",
    "create_schema",
    "read_manifest",
    "redact_secrets",
    "write_manifest",
]
