from __future__ import annotations

from datetime import UTC, datetime

import pytest
from platform_core.clock import FixedClock
from rules_engine import RuleRegistry, default_registry
from security.crypto import LocalDevKeyProvider


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(datetime(2026, 10, 19, 9, 0, tzinfo=UTC))


@pytest.fixture(scope="session")
def rules() -> RuleRegistry:
    return default_registry()


@pytest.fixture
def keys() -> LocalDevKeyProvider:
    return LocalDevKeyProvider()
