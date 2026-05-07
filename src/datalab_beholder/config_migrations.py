"""Config schema migrations.

Each migrator is a pure function that takes the raw dict parsed from YAML
at version N and returns the equivalent dict at version N+1, including
bumping the ``version`` field. ``load_config`` walks the chain from the
file's declared version up to ``LATEST_VERSION``.

The current schema is v1, so ``MIGRATORS`` is empty. When v2 lands, add
``1: migrate_v1_to_v2`` here.
"""

from __future__ import annotations

from collections.abc import Callable

Migrator = Callable[[dict], dict]

MIGRATORS: dict[int, Migrator] = {}
