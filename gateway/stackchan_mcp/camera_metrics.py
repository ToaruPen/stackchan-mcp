"""Small bounded histograms for camera-path diagnostics."""

from __future__ import annotations

import math


class BoundedLatencyHistogram:
    """Aggregate non-negative latency values into bounded coarse buckets."""

    def __init__(self, *, maximum_bucket: int) -> None:
        if maximum_bucket <= 0:
            raise ValueError("maximum histogram bucket must be positive")
        self._maximum_bucket = maximum_bucket
        self._buckets: dict[int, int] = {}
        self._count = 0
        self._maximum = 0

    def add(self, value: int | float) -> None:
        bucket = self._coarse_ceiling(value)
        self._buckets[bucket] = self._buckets.get(bucket, 0) + 1
        self._count += 1
        self._maximum = max(self._maximum, bucket)

    def _coarse_ceiling(self, value: int | float) -> int:
        integer = min(
            self._maximum_bucket,
            max(0, int(math.ceil(value))),
        )
        if integer < 100:
            return integer
        magnitude = 10 ** (len(str(integer)) - 2)
        rounded = math.ceil(integer / magnitude) * magnitude
        return min(self._maximum_bucket, rounded)

    def status(self) -> dict[str, int]:
        return {
            "count": self._count,
            "p50": self._percentile(50),
            "p95": self._percentile(95),
            "p99": self._percentile(99),
            "max": self._maximum,
        }

    def _percentile(self, percentile: int) -> int:
        if self._count == 0:
            return 0
        rank = math.ceil(self._count * percentile / 100)
        seen = 0
        for bucket, count in sorted(self._buckets.items()):
            seen += count
            if seen >= rank:
                return bucket
        return self._maximum
