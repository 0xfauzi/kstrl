"""Tests for the existing token bucket."""

from __future__ import annotations

import pytest
from pulsekit.throttle.token_bucket import BucketRegistry, TokenBucket


def test_a_fresh_bucket_admits_its_whole_burst() -> None:
    bucket = TokenBucket(capacity=3, refill_per_second=1)
    assert [bucket.take(0.0) for _ in range(4)] == [True, True, True, False]


def test_refill_is_capped_at_capacity() -> None:
    bucket = TokenBucket(capacity=2, refill_per_second=1)
    bucket.take(0.0)
    bucket.take(0.0)
    bucket.refill(1000.0)
    assert bucket.tokens == 2


def test_retry_after_reports_the_wait() -> None:
    bucket = TokenBucket(capacity=1, refill_per_second=2)
    assert bucket.take(0.0) is True
    assert bucket.retry_after(0.0) == pytest.approx(0.5)


def test_the_registry_keeps_one_bucket_per_key() -> None:
    registry = BucketRegistry(capacity=1, refill_per_second=1)
    assert registry.bucket_for("a").take(0.0) is True
    assert registry.bucket_for("b").take(0.0) is True
    assert registry.bucket_for("a").take(0.0) is False
