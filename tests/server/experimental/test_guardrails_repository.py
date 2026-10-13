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

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import pytest

from nemoguardrails import RailsConfig
from nemoguardrails.server.experimental import _runtime, rails
from nemoguardrails.server.experimental._application import create_proxy_application
from nemoguardrails.server.experimental._runtime import ServerRuntime
from nemoguardrails.server.experimental.providers.openai.integration import create_openai_chat_integration
from nemoguardrails.server.experimental.rails import ConfigEntry, _repository
from nemoguardrails.server.experimental.rails._adapter import LegacyGuardrailsChecker
from nemoguardrails.server.experimental.rails._repository import GuardrailsRepository
from nemoguardrails.server.experimental.rails.config import validate_config_entries


@pytest.mark.asyncio
async def test_file_loading_does_not_block_the_event_loop(tmp_path, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    config = RailsConfig.from_content(yaml_content="models: []")

    def read_config(_path):
        entered.set()
        release.wait(2)
        finished.set()
        return config

    instance = MagicMock(startup=AsyncMock(), shutdown=AsyncMock())
    monkeypatch.setattr(_repository.RailsConfig, "from_path", read_config)
    monkeypatch.setattr(_repository, "Guardrails", MagicMock(return_value=instance))
    repository = GuardrailsRepository((ConfigEntry("policy", config_path(tmp_path)),))
    load = asyncio.create_task(repository.get("policy"))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        assert not finished.is_set()
    finally:
        release.set()
        await load
        await repository.close()


@pytest.mark.asyncio
async def test_cancelling_one_waiter_does_not_cancel_shared_startup(tmp_path, monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def startup():
        entered.set()
        await release.wait()

    instance = MagicMock(startup=AsyncMock(side_effect=startup), shutdown=AsyncMock())
    monkeypatch.setattr(_repository, "Guardrails", MagicMock(return_value=instance))
    repository = GuardrailsRepository((ConfigEntry("policy", config_path(tmp_path)),))
    first = asyncio.create_task(repository.get("policy"))
    await entered.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    second = asyncio.create_task(repository.get("policy"))
    release.set()
    await second
    await repository.close()
    instance.startup.assert_awaited_once()
    instance.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_shutdown_cancels_pending_startup_and_rejects_new_loads(tmp_path, monkeypatch):
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def startup():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    instance = MagicMock(startup=AsyncMock(side_effect=startup), shutdown=AsyncMock())
    monkeypatch.setattr(_repository, "Guardrails", MagicMock(return_value=instance))
    repository = GuardrailsRepository((ConfigEntry("policy", config_path(tmp_path)),))
    waiting = asyncio.create_task(repository.get("policy"))
    await entered.wait()
    try:
        with anyio.fail_after(0.5):
            await repository.close()
        assert cancelled.is_set()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        with pytest.raises(RuntimeError, match="closed"):
            await repository.get("policy")
    finally:
        for task in repository._loads.values():
            task.cancel()
        waiting.cancel()
        await asyncio.gather(waiting, *repository._loads.values(), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["hang", "raise"])
async def test_shutdown_attempts_all_checkers_and_allows_failed_cleanup_retry(tmp_path, monkeypatch, caplog, failure):
    monkeypatch.setattr(_repository, "_SHUTDOWN_TIMEOUT_SECONDS", 0.01, raising=False)

    async def fail():
        if failure == "hang":
            await asyncio.Event().wait()
        raise RuntimeError("private cleanup detail")

    first = MagicMock(startup=AsyncMock(), shutdown=AsyncMock(side_effect=fail))
    second = MagicMock(startup=AsyncMock(), shutdown=AsyncMock())
    monkeypatch.setattr(_repository, "Guardrails", MagicMock(side_effect=[first, second]))
    path = config_path(tmp_path)
    repository = GuardrailsRepository((ConfigEntry("first", path), ConfigEntry("second", path)))
    await repository.get("first")
    await repository.get("second")
    try:
        with anyio.fail_after(0.5):
            await repository.close()
        first.shutdown.assert_awaited_once()
        second.shutdown.assert_awaited_once()
        assert "cleanup" in caplog.text.lower()
        assert "private cleanup detail" not in caplog.text
        first.shutdown.side_effect = None
        await repository.close()
        assert first.shutdown.await_count == 2
        second.shutdown.assert_awaited_once()
    finally:
        first.shutdown.side_effect = None


@pytest.mark.asyncio
async def test_shutdown_is_shielded_and_concurrent_closes_do_not_duplicate_cleanup(tmp_path, monkeypatch):
    async def shutdown():
        await anyio.lowlevel.checkpoint()

    instance = MagicMock(startup=AsyncMock(), shutdown=AsyncMock(side_effect=shutdown))
    monkeypatch.setattr(_repository, "Guardrails", MagicMock(return_value=instance))
    repository = GuardrailsRepository((ConfigEntry("policy", config_path(tmp_path)),))
    await repository.get("policy")
    with anyio.CancelScope() as scope:
        scope.cancel()
        await repository.close()
    await asyncio.gather(repository.close(), repository.close())
    instance.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_http_client_closes_even_when_checker_shutdown_times_out(tmp_path, monkeypatch):
    monkeypatch.setattr(_repository, "_SHUTDOWN_TIMEOUT_SECONDS", 0.01)

    async def shutdown():
        await asyncio.Event().wait()

    instance = MagicMock(startup=AsyncMock(), shutdown=AsyncMock(side_effect=shutdown))
    monkeypatch.setattr(_repository, "Guardrails", MagicMock(return_value=instance))
    runtime = ServerRuntime.create((ConfigEntry("policy", config_path(tmp_path)),), None)
    await runtime.content_checkers.get("policy")
    with anyio.fail_after(0.5):
        await runtime.close()
    assert runtime.http_client.is_closed
    instance.shutdown.side_effect = None
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["hang", "raise"])
async def test_http_shutdown_is_bounded_and_preserves_primary_failure(monkeypatch, caplog, failure):
    monkeypatch.setattr(_runtime, "_HTTP_SHUTDOWN_TIMEOUT_SECONDS", 0.01)

    async def close_http():
        if failure == "hang":
            await asyncio.Event().wait()
        raise RuntimeError("private HTTP cleanup detail")

    client = MagicMock(aclose=AsyncMock(side_effect=close_http))
    repository = MagicMock(close=AsyncMock())
    runtime = ServerRuntime(client, repository, None)
    with anyio.fail_after(0.5):
        with pytest.raises(ValueError, match="primary failure"):
            try:
                raise ValueError("primary failure")
            finally:
                await runtime.close()
    repository.close.assert_awaited_once()
    client.aclose.assert_awaited_once()
    assert "HTTP client cleanup" in caplog.text
    assert "private HTTP cleanup detail" not in caplog.text


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
