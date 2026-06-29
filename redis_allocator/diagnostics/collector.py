"""Live diagnostics collector for RedisAllocator."""

from __future__ import annotations

import time
from typing import Any

from redis_allocator.diagnostics.invariants import RawAllocatorSnapshot, check_invariants, parse_pool_entry, summarize_pool
from redis_allocator.diagnostics.models import (
    AllocatorDiagnosticsSnapshot,
    BindingDiagnostics,
    IdentityDiagnostics,
    IntegrityDiagnostics,
    OrphanDiagnostics,
    PressureDiagnostics,
    Recommendation,
    RedisDiagnostics,
)


_ATOMIC_POOL_SNAPSHOT_LUA = """
local head = redis.call("GET", KEYS[1]) or ""
local tail = redis.call("GET", KEYS[2]) or ""
local kvs = redis.call("HGETALL", KEYS[3])
local lock_prefix = ARGV[1]
local server_time = tonumber(redis.call("TIME")[1])
local result = {head, tail, server_time}
for i = 1, #kvs, 2 do
    local pool_key = kvs[i]
    local entry_value = kvs[i + 1]
    local lock_key = lock_prefix .. pool_key
    local locked = redis.call("EXISTS", lock_key)
    local ttl = redis.call("TTL", lock_key)
    table.insert(result, pool_key)
    table.insert(result, entry_value)
    table.insert(result, locked)
    table.insert(result, ttl)
end
return result
"""


class RedisAllocatorDiagnostics:
    """Collect diagnostics for one RedisAllocator prefix/suffix pair."""

    def __init__(
        self,
        redis,
        prefix: str,
        suffix: str = "allocator",
        shared: bool = False,
        sample_limit: int = 20,
        scan_limit: int = 1000,
    ):
        self.redis = redis
        self.prefix = prefix
        self.suffix = suffix
        self.shared = shared
        self.sample_limit = max(0, sample_limit)
        self.scan_limit = max(0, scan_limit)
        self._snapshot_script = redis.register_script(_ATOMIC_POOL_SNAPSHOT_LUA)

    def _pool_str(self) -> str:
        return f"{self.prefix}|{self.suffix}|pool"

    def _pool_pointer_str(self, head: bool) -> str:
        return f"{self._pool_str()}|{'head' if head else 'tail'}"

    def _key_str(self, key: str) -> str:
        return f"{self.prefix}|{self.suffix}:{key}"

    def _soft_bind_prefix(self) -> str:
        return f"{self.prefix}|{self.suffix}-cache:bind:"

    def _soft_bind_lock_prefix(self) -> str:
        return self._key_str(self._soft_bind_prefix())

    def _bounded_scan(self, match: str) -> tuple[list[str], int, bool]:
        if self.scan_limit <= 0:
            return [], 0, True

        cursor: int | str = 0
        keys: list[str] = []
        scanned = 0
        count = max(1, min(self.scan_limit, 1000))
        while True:
            cursor, batch = self.redis.scan(cursor=cursor, match=match, count=count)
            for key in batch:
                if scanned >= self.scan_limit:
                    return keys, scanned, True
                keys.append(key)
                scanned += 1
            if cursor in (0, "0"):
                return keys, scanned, False
            if scanned >= self.scan_limit:
                return keys, scanned, True

    def _collect_raw_pool(self) -> RawAllocatorSnapshot:
        raw = self._snapshot_script(
            keys=[
                self._pool_pointer_str(True),
                self._pool_pointer_str(False),
                self._pool_str(),
            ],
            args=[self._key_str("")],
        )
        head = raw[0] or ""
        tail = raw[1] or ""
        now = int(raw[2] or time.time())
        entries = {}
        lock_keys = {}
        lock_ttls = {}
        index = 3
        while index < len(raw):
            key = raw[index]
            value = raw[index + 1]
            locked = bool(raw[index + 2])
            ttl = int(raw[index + 3])
            entries[key] = parse_pool_entry(value)
            lock_keys[key] = locked
            lock_ttls[key] = ttl
            index += 4
        return RawAllocatorSnapshot(
            head=head,
            tail=tail,
            entries=entries,
            lock_keys=lock_keys,
            lock_ttls=lock_ttls,
            shared=self.shared,
            now=now,
        )

    def _collect_bindings(self, raw: RawAllocatorSnapshot) -> BindingDiagnostics:
        prefixes = [self._soft_bind_prefix(), self._soft_bind_lock_prefix()]
        total = 0
        stale = 0
        scanned = 0
        truncated = False
        seen: set[str] = set()
        samples: list[dict[str, Any]] = []
        for prefix in prefixes:
            binding_keys, prefix_scanned, prefix_truncated = self._bounded_scan(f"{prefix}*")
            scanned += prefix_scanned
            truncated = truncated or prefix_truncated
            for binding_key in binding_keys:
                if binding_key in seen:
                    continue
                seen.add(binding_key)
                total += 1
                name = binding_key[len(prefix):]
                value = self.redis.get(binding_key)
                is_stale = value is None or value not in raw.entries or (not self.shared and raw.lock_keys.get(value, False))
                if is_stale:
                    stale += 1
                    if len(samples) < self.sample_limit:
                        samples.append({"name": name, "key": value})
        return BindingDiagnostics(total=total, stale=stale, scanned=scanned, truncated=truncated, samples=samples)

    def _collect_orphans(self, raw: RawAllocatorSnapshot) -> OrphanDiagnostics:
        lock_prefix = self._key_str("")
        lock_keys, scanned, truncated = self._bounded_scan(f"{lock_prefix}*")
        count = 0
        samples: list[str] = []
        for lock_key in lock_keys:
            pool_key = lock_key[len(lock_prefix):]
            if pool_key.startswith(self._soft_bind_prefix()):
                continue
            if pool_key not in raw.entries:
                count += 1
                if len(samples) < self.sample_limit:
                    samples.append(lock_key)
        return OrphanDiagnostics(count=count, scanned=scanned, truncated=truncated, samples=samples)

    def _safe_info(self, section: str) -> dict[str, Any]:
        try:
            return self.redis.info(section)
        except Exception:
            return {}

    def _safe_slowlog(self) -> list[Any]:
        try:
            return self.redis.slowlog_get(self.sample_limit)
        except Exception:
            return []

    def _collect_redis(self) -> RedisDiagnostics:
        memory = self._safe_info("memory")
        clients = self._safe_info("clients")
        commandstats = self._safe_info("commandstats")
        return RedisDiagnostics(
            used_memory=int(memory.get("used_memory", 0) or 0),
            connected_clients=int(clients.get("connected_clients", 0) or 0),
            commandstats=commandstats,
            slowlog=self._safe_slowlog(),
        )

    def _command_calls(self, commandstats: dict[str, Any]) -> dict[str, int]:
        calls: dict[str, int] = {}
        for name, value in commandstats.items():
            command = name.replace("cmdstat_", "")
            if isinstance(value, dict):
                calls[command] = int(value.get("calls", 0) or 0)
            elif isinstance(value, str):
                parts = dict(part.split("=", 1) for part in value.split(",") if "=" in part)
                calls[command] = int(parts.get("calls", 0) or 0)
        return calls

    def _pressure(
        self,
        previous: AllocatorDiagnosticsSnapshot | None,
        current_redis: RedisDiagnostics,
        total_keys: int,
    ) -> PressureDiagnostics:
        if previous is None:
            return PressureDiagnostics()
        interval = max(time.time() - previous.timestamp, 0.0)
        if interval <= 0:
            return PressureDiagnostics()
        previous_calls = self._command_calls(previous.redis.commandstats)
        current_calls = self._command_calls(current_redis.commandstats)
        deltas = {
            command: current_calls.get(command, 0) - previous_calls.get(command, 0)
            for command in sorted(set(previous_calls) | set(current_calls))
        }
        positive_deltas = {command: delta for command, delta in deltas.items() if delta > 0}
        return PressureDiagnostics(
            interval_seconds=interval,
            redis_ops_per_sec=sum(positive_deltas.values()) / interval,
            allocator_keyspace_delta=total_keys - previous.pool.total,
            command_deltas=positive_deltas,
        )

    def _recommendations(
        self,
        integrity: IntegrityDiagnostics,
        pool,
        bindings: BindingDiagnostics,
        orphans: OrphanDiagnostics,
        pressure: PressureDiagnostics,
    ) -> list[Recommendation]:
        recommendations: list[Recommendation] = []
        if not integrity.ok:
            recommendations.append(
                Recommendation("critical", "Stop writers and capture diagnostics JSON before any repair attempt.", "integrity-corrupt")
            )
        if orphans.count > 0:
            recommendations.append(Recommendation("warning", "Orphan locks exist; audit assign/shrink churn and evicted resources.", "orphan-locks"))
        if orphans.truncated or bindings.truncated:
            recommendations.append(Recommendation("info", "Advisory scan was truncated; raise scan_limit for a wider sample.", "scan-truncated"))
        if pool.permanent_locks > 0:
            recommendations.append(Recommendation("info", "Permanent locks exist; audit timeout <= 0 usage.", "permanent-locks"))
        if pool.total > 0 and pool.free / pool.total < 0.1:
            recommendations.append(Recommendation("warning", "Free pool is below 10%; increase capacity or reduce allocation timeout.", "low-free-capacity"))
        if bindings.stale > 0:
            recommendations.append(Recommendation("info", "Stale soft bindings exist; consider unbinding inactive names.", "stale-bindings"))
        if pressure.command_deltas.get("eval", 0) > 100:
            recommendations.append(Recommendation("info", "High EVAL volume observed; inspect allocation rate and dashboard interval.", "high-eval-pressure"))
        return recommendations

    def snapshot(self, previous: AllocatorDiagnosticsSnapshot | None = None) -> AllocatorDiagnosticsSnapshot:
        """Return one diagnostics snapshot."""
        raw = self._collect_raw_pool()
        violations = check_invariants(raw)
        integrity = IntegrityDiagnostics(ok=not violations, violations=violations)
        pool = summarize_pool(raw)
        bindings = self._collect_bindings(raw)
        orphans = self._collect_orphans(raw)
        redis_info = self._collect_redis()
        pressure = self._pressure(previous, redis_info, pool.total)
        recommendations = self._recommendations(integrity, pool, bindings, orphans, pressure)
        if not integrity.ok:
            health = "corrupt"
        elif recommendations:
            health = "degraded"
        else:
            health = "healthy"
        return AllocatorDiagnosticsSnapshot(
            timestamp=time.time(),
            identity=IdentityDiagnostics(prefix=self.prefix, suffix=self.suffix, shared=self.shared),
            health=health,
            integrity=integrity,
            pool=pool,
            bindings=bindings,
            orphans=orphans,
            redis=redis_info,
            pressure=pressure,
            recommendations=recommendations,
        )

    def snapshot_json(self, previous: AllocatorDiagnosticsSnapshot | None = None, indent: int | None = 2) -> str:
        """Return one diagnostics snapshot as JSON."""
        return self.snapshot(previous=previous).to_json(indent=indent)
