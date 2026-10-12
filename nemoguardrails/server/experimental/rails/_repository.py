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
from collections.abc import Sequence
from typing import cast

from nemoguardrails import Guardrails, RailsConfig
from nemoguardrails.server.experimental._content_checker import ContentChecker
from nemoguardrails.server.experimental.rails._adapter import LegacyGuardrailsChecker
from nemoguardrails.server.experimental.rails.config import ConfigEntry, validate_config_entries


class GuardrailsRepository:
    """Lazily load and own legacy Guardrails adapters by config id."""

    def __init__(self, entries: Sequence[ConfigEntry]):
        resolved_entries = validate_config_entries(entries)
        self._config_paths = {entry.id: entry.config_path for entry in resolved_entries}
        self._loads: dict[str, asyncio.Task[LegacyGuardrailsChecker]] = {}
        self._lock = asyncio.Lock()

    async def _load(self, config_id: str) -> LegacyGuardrailsChecker:
        config = RailsConfig.from_path(str(self._config_paths[config_id]))
        rails = Guardrails(config=config, require_iorails=True)
        await rails.startup()
        return LegacyGuardrailsChecker(rails)

    async def get(self, config_id: str) -> ContentChecker:
        if config_id not in self._config_paths:
            raise ValueError(f"Unknown Rails config: {config_id}")
        load = self._loads.get(config_id)
        if load is not None:
            return await asyncio.shield(load)
        async with self._lock:
            load = self._loads.get(config_id)
            if load is None:
                load = asyncio.create_task(self._load(config_id))
                self._loads[config_id] = load
        return await asyncio.shield(load)

    async def close(self) -> None:
        loads = tuple(self._loads.values())
        self._loads.clear()
        if not loads:
            return
        results = await asyncio.gather(*loads, return_exceptions=True)
        checkers = [
            cast(LegacyGuardrailsChecker, result) for result in results if not isinstance(result, BaseException)
        ]
        if checkers:
            await asyncio.gather(*(checker.close() for checker in checkers))
