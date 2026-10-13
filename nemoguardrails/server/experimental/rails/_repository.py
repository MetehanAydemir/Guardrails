# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Load and own Guardrails content-checking adapters."""

import asyncio
import logging
from collections.abc import Sequence

import anyio

from nemoguardrails import Guardrails, RailsConfig
from nemoguardrails.server.experimental._content_checker import ContentChecker
from nemoguardrails.server.experimental.rails._adapter import LegacyGuardrailsChecker
from nemoguardrails.server.experimental.rails.config import ConfigEntry, validate_config_entries

log = logging.getLogger(__name__)
_SHUTDOWN_TIMEOUT_SECONDS = 5.0


class GuardrailsRepository:
    """Own one lazy startup task per trusted config, independently of callers.

    File parsing runs in a worker thread; engine construction and startup stay
    on the application loop. Cancelling a request does not cancel shared startup.
    Shutdown rejects new work, cancels pending startup, and gives each owner a
    shielded, cooperative cleanup budget. Failed cleanup remains retryable.
    """

    def __init__(self, entries: Sequence[ConfigEntry]):
        resolved_entries = validate_config_entries(entries)
        self._config_paths = {entry.id: entry.config_path for entry in resolved_entries}
        self._loads: dict[str, asyncio.Task[LegacyGuardrailsChecker]] = {}
        self._closed = False
        self._close_lock = asyncio.Lock()

    async def _load(self, config_id: str) -> LegacyGuardrailsChecker:
        config = await asyncio.to_thread(RailsConfig.from_path, str(self._config_paths[config_id]))
        rails = Guardrails(config=config, require_iorails=True)
        await rails.startup()
        return LegacyGuardrailsChecker(rails)

    async def get(self, config_id: str) -> ContentChecker:
        """Share startup and cached failures without transferring task ownership."""

        if self._closed:
            raise RuntimeError("The Guardrails repository is closed.")
        if config_id not in self._config_paths:
            raise ValueError(f"Unknown Rails config: {config_id}")
        load = self._loads.get(config_id)
        if load is None:
            load = asyncio.create_task(self._load(config_id))
            self._loads[config_id] = load
        return await asyncio.shield(load)

    async def _close_load(self, config_id: str, load: asyncio.Task[LegacyGuardrailsChecker]) -> None:
        """Cancel unfinished startup or close its result without blocking peers."""

        try:
            with anyio.move_on_after(_SHUTDOWN_TIMEOUT_SECONDS, shield=True) as scope:
                if not load.done():
                    load.cancel()
                try:
                    checker = await load
                except asyncio.CancelledError:
                    if not load.cancelled():
                        raise
                except Exception:
                    pass
                else:
                    await checker.close()
            if scope.cancelled_caught:
                log.warning("Guardrails cleanup timed out; resources may remain open.")
            else:
                self._loads.pop(config_id, None)
        except Exception:
            log.warning("Guardrails cleanup failed; resources may remain open.")

    async def close(self) -> None:
        """Attempt every owner independently; never restart a closed repository.

        Five seconds is a cooperative per-owner budget, not forced termination
        of blocking code or cancellation-suppressing engines. Successful owners
        are forgotten; another close call may retry incomplete cleanup.
        """

        self._closed = True
        with anyio.CancelScope(shield=True):
            async with self._close_lock:
                async with anyio.create_task_group() as group:
                    for config_id, load in tuple(self._loads.items()):
                        group.start_soon(self._close_load, config_id, load)
