"""A token bucket, and the per-key registry the admin surface throttles with.

The bucket is a plain accumulator: it refills at a constant rate up to a
capacity, and a request takes tokens or is refused. The caller supplies
the clock reading, so nothing here reads the wall clock and every test is
deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TokenBucket:
    """Refills at ``refill_per_second`` up to ``capacity`` tokens.

    ``capacity`` is the burst: a bucket that has been idle admits that
    many requests at once. The sustained rate is ``refill_per_second``.
    """

    capacity: float
    refill_per_second: float
    tokens: float = 0.0
    updated_at: float = 0.0

    def __post_init__(self) -> None:
        if self.capacity <= 0:
            raise ValueError("capacity must be positive")
        if self.refill_per_second <= 0:
            raise ValueError("refill_per_second must be positive")
        if not self.tokens:
            self.tokens = self.capacity

    def take(self, now: float, cost: float = 1.0) -> bool:
        """Spend ``cost`` tokens at time ``now``; False when short."""
        self.refill(now)
        if self.tokens < cost:
            return False
        self.tokens -= cost
        return True

    def refill(self, now: float) -> None:
        """Advance the bucket to ``now``. Going backwards does nothing."""
        elapsed = now - self.updated_at
        if elapsed <= 0:
            return
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_second)
        self.updated_at = now

    def retry_after(self, now: float, cost: float = 1.0) -> float:
        """Seconds until ``cost`` tokens are available, 0.0 when they are."""
        self.refill(now)
        if self.tokens >= cost:
            return 0.0
        return (cost - self.tokens) / self.refill_per_second


@dataclass
class BucketRegistry:
    """One bucket per key, created on first use from a shared shape."""

    capacity: float
    refill_per_second: float
    buckets: dict[str, TokenBucket] = field(default_factory=dict)

    def bucket_for(self, key: str) -> TokenBucket:
        """The bucket for ``key``, created on first use."""
        bucket = self.buckets.get(key)
        if bucket is None:
            bucket = TokenBucket(
                capacity=self.capacity,
                refill_per_second=self.refill_per_second,
            )
            self.buckets[key] = bucket
        return bucket

    def forget(self, key: str) -> None:
        """Drop ``key``'s bucket, so the next request starts at full burst."""
        self.buckets.pop(key, None)
