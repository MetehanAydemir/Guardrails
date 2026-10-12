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

"""Forward provider exchanges through an application-owned HTTP client."""

import math
from collections.abc import AsyncIterator, Callable, Collection
from typing import cast

import httpx
from fastapi import Request
from starlette.responses import Response

from nemoguardrails.server.experimental._http_kernel import (
    DEFAULT_MAX_RESPONSE_BODY_BYTES,
    BufferedHttpRequest,
    BufferedHttpResponse,
    HttpDispatchFailed,
    HttpHeaders,
    ResponseBodyTooLarge,
    _end_to_end_headers,
    _render_response,
)
from nemoguardrails.server.experimental._http_paths import _strip_mount_prefix
from nemoguardrails.server.experimental._httpx_response import _HttpxResponseBody
from nemoguardrails.server.experimental._streaming_http import StreamingHttpResponse, _ClosingStreamingResponse
from nemoguardrails.server.experimental.provider.integration import UpstreamSettings

DEFAULT_OUTBOUND_TIMEOUT = 30.0

_HOP_BY_HOP_HEADERS = frozenset(
    {
        b"connection",
        b"keep-alive",
        b"proxy-authenticate",
        b"proxy-authorization",
        b"proxy-connection",
        b"te",
        b"trailer",
        b"transfer-encoding",
        b"upgrade",
    }
)


def _connection_tokens(headers: HttpHeaders) -> set[bytes]:
    """Collect case-insensitive header names nominated by Connection fields."""

    tokens = set()
    for name, value in headers:
        if name.lower() == b"connection":
            tokens.update(token.strip().lower() for token in value.split(b",") if token.strip())
    return tokens


def _request_headers(
    headers: HttpHeaders,
    control_headers: frozenset[bytes],
    *,
    require_identity_response: bool,
) -> list[tuple[bytes, bytes]]:
    """Remove connection and proxy-control headers; let HTTPX rebuild host and framing."""

    excluded = (
        _HOP_BY_HOP_HEADERS
        | _connection_tokens(headers)
        | control_headers
        | {
            b"host",
            b"content-length",
        }
    )
    if require_identity_response:
        excluded = excluded | {b"accept-encoding"}
    forwarded = [(name, value) for name, value in headers if name.lower() not in excluded]
    if require_identity_response:
        forwarded.append((b"accept-encoding", b"identity"))
    return forwarded


def _response_headers(headers: httpx.Headers) -> HttpHeaders:
    """Lowercase ASGI response names, preserving end-to-end values and order."""

    return tuple((name.lower(), value) for name, value in _end_to_end_headers(tuple(headers.raw)))


HTTPClientResolver = httpx.AsyncClient | Callable[[], httpx.AsyncClient]


class ProviderTransport:
    """Dispatch one provider instance through a caller-owned HTTPX client.

    The transport maps paths beneath a provider mount to its upstream, strips
    connection-specific and proxy control headers, and supports bounded
    guarded exchanges plus unbounded streaming pass-through exchanges. It
    never owns or closes the injected client. Request headers and cookies come
    only from the current caller, not client defaults or authentication.
    Expected HTTPX network failures become HttpDispatchFailed.
    """

    def __init__(
        self,
        upstream: UpstreamSettings,
        http_client: HTTPClientResolver,
        *,
        control_headers: Collection[str] = (),
        max_response_body_bytes: int = DEFAULT_MAX_RESPONSE_BODY_BYTES,
        timeout: float = DEFAULT_OUTBOUND_TIMEOUT,
    ):
        """Validate the fixed upstream mapping, response bound, and timeout."""

        if not isinstance(http_client, httpx.AsyncClient) and not callable(http_client):
            raise TypeError("The outbound HTTP client must be an httpx.AsyncClient or a callable resolver.")
        parsed_base_url = httpx.URL(upstream.base_url)
        if not parsed_base_url.is_absolute_url or parsed_base_url.scheme not in {"http", "https"}:
            raise ValueError("The outbound base URL must be an absolute HTTP or HTTPS URL.")
        if parsed_base_url.userinfo:
            raise ValueError("The outbound base URL must not contain credentials.")
        if parsed_base_url.query or parsed_base_url.fragment:
            raise ValueError("The outbound base URL must not contain a query or fragment.")
        normalized_mount = upstream.mount_path.rstrip("/")
        if normalized_mount and (not normalized_mount.startswith("/") or not normalized_mount.isascii()):
            raise ValueError("The outbound mount path must be an absolute ASCII path.")
        if any(
            not isinstance(name, str) or not name or not name.isascii() or name != name.strip().lower()
            for name in control_headers
        ):
            raise ValueError("Outbound control header names must be non-empty, lowercase ASCII strings.")
        if (
            type(max_response_body_bytes) is not int
            or max_response_body_bytes <= 0
            or not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or timeout <= 0
            or not math.isfinite(timeout)
        ):
            raise ValueError("The outbound response limit and timeout must be positive.")
        self._http_client = http_client
        self._base_url = parsed_base_url
        self._mount_path = normalized_mount
        self._control_headers = frozenset(name.encode("ascii") for name in control_headers)
        self._max_response_body_bytes = max_response_body_bytes
        self._timeout = float(timeout)

    def _client(self) -> httpx.AsyncClient:
        if isinstance(self._http_client, httpx.AsyncClient):
            client = self._http_client
        else:
            client = cast(Callable[[], httpx.AsyncClient], self._http_client)()
        if not isinstance(client, httpx.AsyncClient):
            raise TypeError("The outbound HTTP client resolver must return an httpx.AsyncClient.")
        return client

    def upstream_url(self, path: str, raw_path: bytes, query: bytes = b"") -> httpx.URL:
        """Apply the base and mount mapping without reencoding suffix or query bytes."""

        if not path.startswith("/") or not raw_path.startswith(b"/") or b"?" in raw_path:
            raise ValueError("An outbound request must contain an absolute path without a query.")
        if self._mount_path:
            raw_path = _strip_mount_prefix(path, raw_path, self._mount_path)
            path = path[len(self._mount_path) :] or "/"
        base_path = self._base_url.path.rstrip("/")
        if base_path and (path == base_path or path.startswith(base_path + "/")):
            upstream_path = raw_path
        else:
            raw_base_path = self._base_url.raw_path.rstrip(b"/")
            upstream_path = raw_base_path + raw_path
        if query:
            upstream_path += b"?" + query
        return self._base_url.copy_with(raw_path=upstream_path)

    async def _open(
        self,
        *,
        method: str,
        path: str,
        raw_path: bytes,
        query: bytes,
        headers: HttpHeaders,
        body: bytes | AsyncIterator[bytes],
        require_identity_response: bool,
    ) -> httpx.Response:
        """Send caller bytes and headers without inherited headers, cookies, or auth.

        Redirects are disabled. Expected HTTPX request failures become
        HttpDispatchFailed. The returned response remains open and its cleanup
        belongs to the receiving caller.
        """

        client = self._client()
        upstream_request = httpx.Request(
            method,
            self.upstream_url(path, raw_path, query),
            headers=_request_headers(
                headers,
                self._control_headers,
                require_identity_response=require_identity_response,
            ),
            content=body,
            extensions={"timeout": httpx.Timeout(self._timeout).as_dict()},
        )
        try:
            return await client.send(upstream_request, stream=True, follow_redirects=False, auth=None)
        except httpx.RequestError as failure:
            raise HttpDispatchFailed("The upstream request failed.") from failure

    async def _open_buffered(
        self,
        request: BufferedHttpRequest,
        *,
        require_identity_response: bool,
    ) -> httpx.Response:
        return await self._open(
            method=request.method,
            path=request.path,
            raw_path=request.raw_path,
            query=request.query,
            headers=request.headers,
            body=request.body,
            require_identity_response=require_identity_response,
        )

    async def _buffer(self, response: httpx.Response) -> BufferedHttpResponse:
        """Bound raw response reads and attempt closure on every exit path.

        Raise ResponseBodyTooLarge above the configured limit and translate
        HTTPX read failures to HttpDispatchFailed.
        """

        body = _HttpxResponseBody(response)
        chunks = []
        size = 0
        try:
            async for chunk in body:
                size += len(chunk)
                if size > self._max_response_body_bytes:
                    raise ResponseBodyTooLarge
                chunks.append(chunk)
        except httpx.RequestError as failure:
            raise HttpDispatchFailed("The upstream response could not be read.") from failure
        finally:
            await body.aclose()
        return BufferedHttpResponse(response.status_code, _response_headers(response.headers), b"".join(chunks))

    async def dispatch(
        self,
        request: BufferedHttpRequest,
        *,
        require_identity_response: bool = True,
    ) -> BufferedHttpResponse:
        """Send and buffer one response within the limit, attempting closure before return."""

        return await self._buffer(
            await self._open_buffered(request, require_identity_response=require_identity_response)
        )

    async def stream_dispatch(
        self,
        request: BufferedHttpRequest,
        *,
        require_identity_response: bool = True,
    ) -> StreamingHttpResponse | BufferedHttpResponse:
        """Transfer a successful response's byte source or buffer a non-success reply.

        The caller must close a returned streaming body even if never iterated.
        Non-success responses use the configured buffering limit and cleanup.
        """

        response = await self._open_buffered(
            request,
            require_identity_response=require_identity_response,
        )
        if 200 <= response.status_code <= 299:
            body = _HttpxResponseBody(response)
            try:
                return StreamingHttpResponse(response.status_code, _response_headers(response.headers), body)
            except BaseException:
                await body.aclose()
                raise
        return await self._buffer(response)

    async def passthrough(self, request: Request) -> Response:
        """Stream an unrelated provider operation without guarded body limits."""

        raw_path = request.scope.get("raw_path")
        if not isinstance(raw_path, bytes):
            raw_path = httpx.URL(path=request.url.path).raw_path
        query = request.scope.get("query_string", b"")
        if not isinstance(query, bytes):
            raise TypeError("The downstream query string must be bytes.")
        response = await self._open(
            method=request.method,
            path=request.url.path,
            raw_path=raw_path,
            query=query,
            headers=tuple(request.scope.get("headers", ())),
            body=request.stream(),
            require_identity_response=False,
        )
        body = _HttpxResponseBody(response)
        try:
            if request.method == "HEAD" or response.status_code < 200 or response.status_code in {204, 205, 304}:
                await body.aclose()
                return _render_response(
                    BufferedHttpResponse(response.status_code, _response_headers(response.headers), b""), request.method
                )
            downstream = _ClosingStreamingResponse(body, body, status_code=response.status_code)
            downstream.raw_headers = [
                (name, value)
                for name, value in _response_headers(response.headers)
                if name.lower() != b"content-length"
            ]
            return downstream
        except BaseException:
            await body.aclose()
            raise
