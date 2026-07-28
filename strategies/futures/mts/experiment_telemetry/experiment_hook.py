"""Stub — prevents ImportError in test environment."""
import logging
from typing import Any

logger = logging.getLogger(__name__)

def observe_release_decision(*args: Any, **kwargs: Any) -> None:
    pass

def initialize_experiment(*args: Any, **kwargs: Any) -> None:
    pass
