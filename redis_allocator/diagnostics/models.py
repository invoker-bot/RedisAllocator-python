"""JSON-serializable diagnostics models."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any


def _to_plain(value: Any) -> Any:
    """Convert dataclasses, lists, and dictionaries into JSON-safe values."""
    if is_dataclass(value):
        return {key: _to_plain(item) for key, item in asdict(value).items()}
    if isinstance(value, list):
        return [_to_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_plain(item) for key, item in value.items()}
    return value


class JsonModel:
    """Mixin for diagnostics models that can be encoded as JSON."""

    def to_dict(self) -> dict[str, Any]:
        """Return a plain dictionary representation."""
        return _to_plain(self)

    def to_json(self, indent: int | None = None) -> str:
        """Return a JSON string representation."""
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)


@dataclass
class IdentityDiagnostics(JsonModel):
    """Allocator identity for a diagnostics snapshot."""

    prefix: str
    suffix: str
    shared: bool


@dataclass
class IntegrityDiagnostics(JsonModel):
    """Structural integrity status."""

    ok: bool = True
    violations: list[str] = field(default_factory=list)


@dataclass
class PoolDiagnostics(JsonModel):
    """Pool capacity and lock counters."""

    total: int = 0
    free: int = 0
    allocated_markers: int = 0
    locked: int = 0
    expired_entries: int = 0
    permanent_locks: int = 0
    reclaimable: int = 0


@dataclass
class BindingDiagnostics(JsonModel):
    """Soft-binding counters and bounded samples."""

    total: int = 0
    stale: int = 0
    samples: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class OrphanDiagnostics(JsonModel):
    """Lock keys that no longer belong to the allocator pool."""

    count: int = 0
    samples: list[str] = field(default_factory=list)


@dataclass
class RedisDiagnostics(JsonModel):
    """Redis-level advisory metrics."""

    used_memory: int = 0
    connected_clients: int = 0
    commandstats: dict[str, Any] = field(default_factory=dict)
    slowlog: list[Any] = field(default_factory=list)


@dataclass
class PressureDiagnostics(JsonModel):
    """Pressure deltas between two diagnostics snapshots."""

    interval_seconds: float = 0.0
    redis_ops_per_sec: float = 0.0
    allocator_keyspace_delta: int = 0
    command_deltas: dict[str, int] = field(default_factory=dict)


@dataclass
class Recommendation(JsonModel):
    """Actionable diagnostics recommendation."""

    severity: str
    message: str
    code: str


@dataclass
class AllocatorDiagnosticsSnapshot(JsonModel):
    """Complete allocator diagnostics snapshot."""

    timestamp: float
    identity: IdentityDiagnostics
    health: str
    integrity: IntegrityDiagnostics = field(default_factory=IntegrityDiagnostics)
    pool: PoolDiagnostics = field(default_factory=PoolDiagnostics)
    bindings: BindingDiagnostics = field(default_factory=BindingDiagnostics)
    orphans: OrphanDiagnostics = field(default_factory=OrphanDiagnostics)
    redis: RedisDiagnostics = field(default_factory=RedisDiagnostics)
    pressure: PressureDiagnostics = field(default_factory=PressureDiagnostics)
    recommendations: list[Recommendation] = field(default_factory=list)
