# 2026-07-24 Gemini CLI: Wave 0 Policy Registry
from typing import Any, Type
from .contracts import ExitFamily
from .policy import ExitPolicy


class PolicyRegistry:
    """Registry mapping (ExitFamily, version) to concrete ExitPolicy classes."""

    _registry: dict[tuple[ExitFamily, str], Type[ExitPolicy[Any, Any]]] = {}

    @classmethod
    def register(cls, family: ExitFamily, version: str, policy_cls: Type[ExitPolicy[Any, Any]]) -> None:
        """Register a policy implementation class."""
        key = (family, version)
        if key in cls._registry:
            raise ValueError(f"Policy already registered for key: {key}")
        cls._registry[key] = policy_cls

    @classmethod
    def get(cls, family: ExitFamily, version: str) -> Type[ExitPolicy[Any, Any]]:
        """Retrieve policy implementation class."""
        key = (family, version)
        if key not in cls._registry:
            raise KeyError(f"No policy registered for key: {key}")
        return cls._registry[key]

    @classmethod
    def clear(cls) -> None:
        """Clear all registered policies (for test isolation)."""
        cls._registry.clear()
