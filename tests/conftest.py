"""Pytest bootstrap helpers for environment-specific test stability.

This repo imports several modules that use ``@numba.njit(cache=True)`` at import
time. In the current local environment, Numba cannot create a cache locator for
some source paths during pytest collection, which causes collection to fail
before any tests run.

For tests only, strip the ``cache=True`` flag from ``numba.njit``/``numba.jit``.
This preserves the compiled behavior while avoiding environment-specific cache
initialization failures. Production code remains unchanged.
"""

from __future__ import annotations

import os

import numba as nb


def _strip_cache_flag(decorator):
    def wrapped(*args, **kwargs):
        if kwargs.get("cache") is True:
            kwargs = dict(kwargs)
            kwargs.pop("cache", None)
        return decorator(*args, **kwargs)

    return wrapped


nb.njit = _strip_cache_flag(nb.njit)
nb.jit = _strip_cache_flag(nb.jit)

# Shioaji imports configure a file logger at import time. In the sandboxed test
# environment, writing under the repo root is not allowed, so redirect broker
# logs to a writable temp location before any module imports `shioaji`.
os.environ.setdefault("SJ_LOG_PATH", "/tmp/shioaji.log")
