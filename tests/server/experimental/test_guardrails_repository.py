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

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nemoguardrails.server.experimental import rails
from nemoguardrails.server.experimental._application import create_proxy_application
from nemoguardrails.server.experimental.providers.openai.integration import create_openai_chat_integration
from nemoguardrails.server.experimental.rails import ConfigEntry
from nemoguardrails.server.experimental.rails._adapter import LegacyGuardrailsChecker
from nemoguardrails.server.experimental.rails._repository import GuardrailsRepository
from nemoguardrails.server.experimental.rails.config import validate_config_entries


def config_path(tmp_path):
    path = tmp_path / "rails"
    path.mkdir(exist_ok=True)
    (path / "config.yml").write_text("models: []\n", encoding="utf-8")
    return path


def test_rails_api_exports_only_config_resolution_types():
    expected = {"ConfigContext", "ConfigEntry", "ConfigResolver"}

    assert set(rails.__all__) == expected
    assert all(getattr(rails, name) is not None for name in expected)


@pytest.mark.asyncio
async def test_repository_caches_and_closes_guardrails_by_config_id(tmp_path):
    guardrails = MagicMock()
    guardrails.startup = AsyncMock()
    guardrails.shutdown = AsyncMock()
    repository = GuardrailsRepository((ConfigEntry("content-safety", config_path(tmp_path)),))

    with (
        patch("nemoguardrails.server.experimental.rails._repository.RailsConfig.from_path", return_value=MagicMock()),
        patch(
            "nemoguardrails.server.experimental.rails._repository.Guardrails",
            return_value=guardrails,
        ) as guardrails_class,
    ):
        first = await repository.get("content-safety")
        second = await repository.get("content-safety")
        await repository.close()

    assert isinstance(first, LegacyGuardrailsChecker)
    assert second is first
    guardrails_class.assert_called_once()
    guardrails.startup.assert_awaited_once()
    guardrails.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_repository_does_not_share_cores_between_config_ids(tmp_path):
    path = config_path(tmp_path)
    guardrails = [MagicMock(), MagicMock()]
    for instance in guardrails:
        instance.startup = AsyncMock()
        instance.shutdown = AsyncMock()
    repository = GuardrailsRepository((ConfigEntry("tenant-a", path), ConfigEntry("tenant-b", path)))

    with (
        patch("nemoguardrails.server.experimental.rails._repository.RailsConfig.from_path", return_value=MagicMock()),
        patch("nemoguardrails.server.experimental.rails._repository.Guardrails", side_effect=guardrails),
    ):
        first = await repository.get("tenant-a")
        second = await repository.get("tenant-b")
        await repository.close()

    assert first is not second
    for instance in guardrails:
        instance.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_repository_caches_startup_failures(tmp_path):
    guardrails = MagicMock()
    guardrails.startup = AsyncMock(side_effect=RuntimeError("broken config"))
    repository = GuardrailsRepository((ConfigEntry("broken", config_path(tmp_path)),))

    with (
        patch("nemoguardrails.server.experimental.rails._repository.RailsConfig.from_path", return_value=MagicMock()),
        patch("nemoguardrails.server.experimental.rails._repository.Guardrails", return_value=guardrails),
    ):
        for _ in range(2):
            with pytest.raises(RuntimeError, match="broken config"):
                await repository.get("broken")
        await repository.close()

    guardrails.startup.assert_awaited_once()


@pytest.mark.asyncio
async def test_repository_rejects_unknown_config(tmp_path):
    repository = GuardrailsRepository((ConfigEntry("known", config_path(tmp_path)),))

    with pytest.raises(ValueError, match="Unknown Rails config"):
        await repository.get("unknown")


def test_configs_are_validated_at_application_construction(tmp_path):
    path = config_path(tmp_path)

    with pytest.raises(ValueError, match="Duplicate Rails config"):
        validate_config_entries((ConfigEntry("duplicate", path), ConfigEntry("duplicate", path)))
    with pytest.raises(ValueError, match="configuration directory"):
        validate_config_entries((ConfigEntry("missing", tmp_path / "missing"),))


def test_multiple_configs_require_a_resolver(tmp_path):
    path = config_path(tmp_path)

    with pytest.raises(ValueError, match="require a resolver"):
        create_proxy_application(
            providers=(create_openai_chat_integration(),),
            rails_configs=(ConfigEntry("first", path), ConfigEntry("second", path)),
        )


def test_resolver_requires_a_config_entry():
    with pytest.raises(ValueError, match="requires at least one config entry"):
        create_proxy_application(
            providers=(create_openai_chat_integration(),),
            config_resolver=lambda _context: "missing",
        )
