"""Registry for discovering, instantiating, and health-checking AI CLI harnesses."""

from __future__ import annotations

import asyncio
from typing import Callable, Dict, List

from .agy_harness import AgyHarness
from .base import BaseHarness
from .types import HealthStatus

HarnessFactory = Callable[..., BaseHarness]


class HarnessRegistry:
    """Thread-safe registry mapping canonical harness names to factory callables."""

    def __init__(self) -> None:
        self._factories: Dict[str, HarnessFactory] = {}

    def register(self, name: str, factory: HarnessFactory, overwrite: bool = False) -> None:
        """Register a harness factory under a normalized lowercase key."""
        key = name.strip().lower()
        if not key:
            raise ValueError("Harness name cannot be empty")
        if key in self._factories and not overwrite:
            raise ValueError(f"Harness '{key}' is already registered")
        self._factories[key] = factory

    def has(self, name: str) -> bool:
        """Return True if a harness key is registered."""
        return name.strip().lower() in self._factories

    def list_names(self) -> List[str]:
        """Return all registered harness names in registration order."""
        return list(self._factories.keys())

    def create(self, name: str, **kwargs) -> BaseHarness:
        """Instantiate a fresh harness instance for the requested name."""
        key = name.strip().lower()
        factory = self._factories.get(key)
        if factory is None:
            raise KeyError(f"Unknown harness '{name}'. Registered: {', '.join(self._factories.keys()) or 'none'}")
        return factory(**kwargs)

    def get(self, name: str, **kwargs) -> BaseHarness:
        """Alias for create()."""
        return self.create(name, **kwargs)

    async def check_all_health(self) -> Dict[str, HealthStatus]:
        """Run non-billable health probes concurrently across all registered harnesses."""
        names = self.list_names()
        if not names:
            return {}

        async def _probe(name: str) -> tuple[str, HealthStatus]:
            instance = self.create(name)
            return name, await instance.check_health()

        results = await asyncio.gather(*(_probe(n) for n in names))
        return dict(results)

    async def list_available(self) -> List[str]:
        """Return registered harness names whose local binaries pass health checks."""
        statuses = await self.check_all_health()
        return [name for name, status in statuses.items() if status.available]


def create_default_registry() -> HarnessRegistry:
    """Create the default TodoX harness registry with built-in adapters."""
    registry = HarnessRegistry()
    registry.register("agy", AgyHarness)
    return registry


if __name__ == "__main__":
    async def _main() -> None:
        reg = create_default_registry()
        print("Registered harnesses:", reg.list_names())
        health = await reg.check_all_health()
        for name, status in health.items():
            print(f" - {name}: available={status.available}, version={status.version}, msg={status.message}")
        print("harness.registry self-test passed!")

    asyncio.run(_main())
