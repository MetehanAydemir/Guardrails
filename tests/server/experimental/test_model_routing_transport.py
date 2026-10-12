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

import json

import httpx
import pytest

from examples.scripts.model_routing_transport import ModelRoute, ModelRoutingTransport


class RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.requests = []
        self.close_calls = 0

    async def handle_async_request(self, request):
        self.requests.append(request)
        return httpx.Response(200, json={"ok": True}, request=request)

    async def aclose(self):
        self.close_calls += 1


def request(model_marker=...):
    payload = {"messages": [{"role": "user", "content": "hello"}]}
    if model_marker is not ...:
        payload["model"] = model_marker
    return httpx.Request(
        "POST",
        "http://model-router.internal/v1/files/a%2Fb?trace=enabled%2Fverbose",
        headers={
            "authorization": "Bearer caller-credential",
            "content-type": "application/json",
            "content-digest": "caller-signature",
        },
        json=payload,
    )


def routes():
    return {
        "canonical-a": ModelRoute(
            httpx.URL("https://provider-a.example/v1"),
            "deployment-a",
            "PROVIDER_A_KEY",
        ),
        "canonical-b": ModelRoute(
            httpx.URL("https://provider-b.example/openai/v1"),
            "vendor/deployment-b",
            "PROVIDER_B_KEY",
        ),
    }


@pytest.mark.asyncio
async def test_alias_selects_trusted_origin_and_rewrites_model_and_credential(monkeypatch):
    monkeypatch.setenv("PROVIDER_A_KEY", "destination-a")
    network = RecordingTransport()
    transport = ModelRoutingTransport(
        routes(),
        {"public-a": "canonical-a"},
        default_model="canonical-a",
        network_transport=network,
    )
    routed = request("public-a")
    for name in ("api-key", "x-api-key", "cookie"):
        routed.headers[name] = "caller-secret"
    payload = json.loads(routed.content)
    payload["base_url"] = "http://client-selected.invalid"
    routed = httpx.Request(routed.method, routed.url, headers=routed.headers, json=payload)

    response = await transport.handle_async_request(routed)

    assert response.status_code == 200
    assert len(network.requests) == 1
    upstream = network.requests[0]
    assert str(upstream.url) == "https://provider-a.example/v1/files/a%2Fb?trace=enabled%2Fverbose"
    assert json.loads(upstream.content) == {
        "messages": [{"role": "user", "content": "hello"}],
        "model": "deployment-a",
        "base_url": "http://client-selected.invalid",
    }
    assert upstream.headers["authorization"] == "Bearer destination-a"
    assert all(name not in upstream.headers for name in ("api-key", "x-api-key", "cookie"))
    assert "content-digest" not in upstream.headers


@pytest.mark.asyncio
async def test_each_destination_receives_only_its_own_credential(monkeypatch):
    monkeypatch.setenv("PROVIDER_A_KEY", "destination-a")
    monkeypatch.setenv("PROVIDER_B_KEY", "destination-b")
    network = RecordingTransport()
    transport = ModelRoutingTransport(
        routes(),
        {"public-a": "canonical-a", "public-b": "canonical-b"},
        default_model="canonical-a",
        network_transport=network,
    )

    await transport.handle_async_request(request("public-a"))
    await transport.handle_async_request(request("public-b"))

    assert [item.url.host for item in network.requests] == ["provider-a.example", "provider-b.example"]
    assert [item.headers["authorization"] for item in network.requests] == [
        "Bearer destination-a",
        "Bearer destination-b",
    ]
    assert all(item.headers["authorization"] != "Bearer caller-credential" for item in network.requests)


@pytest.mark.asyncio
async def test_model_less_request_uses_explicit_default_route(monkeypatch):
    monkeypatch.setenv("PROVIDER_B_KEY", "destination-b")
    network = RecordingTransport()
    transport = ModelRoutingTransport(
        routes(),
        {},
        default_model="canonical-b",
        network_transport=network,
    )

    response = await transport.handle_async_request(request())

    assert response.status_code == 200
    assert network.requests[0].url.host == "provider-b.example"
    assert "model" not in json.loads(network.requests[0].content)
    assert network.requests[0].headers["authorization"] == "Bearer destination-b"


@pytest.mark.asyncio
async def test_unknown_model_and_missing_route_credential_stop_before_network(monkeypatch):
    monkeypatch.delenv("PROVIDER_A_KEY", raising=False)
    network = RecordingTransport()
    transport = ModelRoutingTransport(
        routes(),
        {},
        default_model="canonical-a",
        network_transport=network,
    )

    unknown = await transport.handle_async_request(request("client-selected-model"))
    missing_key = await transport.handle_async_request(request("canonical-a"))

    assert unknown.status_code == 404
    assert unknown.json()["error"]["type"] == "model_routing_error"
    assert missing_key.status_code == 502
    assert "PROVIDER_A_KEY" in missing_key.json()["error"]["message"]
    assert network.requests == []


@pytest.mark.asyncio
async def test_transport_closes_its_owned_network_transport():
    network = RecordingTransport()
    transport = ModelRoutingTransport(
        routes(),
        {},
        default_model="canonical-a",
        network_transport=network,
    )

    await transport.aclose()

    assert network.close_calls == 1


@pytest.mark.parametrize(
    "url",
    [
        "provider.example/v1",
        "ftp://provider.example/v1",
        "https://user:secret@provider.example/v1",
        "https://provider.example/v1?destination=other",
        "https://provider.example/v1#fragment",
    ],
)
def test_route_destinations_must_be_fixed_safe_http_origins(url):
    with pytest.raises(ValueError, match="base URL"):
        ModelRoute(httpx.URL(url), "deployment")
