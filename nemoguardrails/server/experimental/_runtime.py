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

"""Own application-lifespan resources for the private provider proxy."""

from dataclasses import dataclass
from http.cookiejar import Cookie, CookieJar, DefaultCookiePolicy
from typing import Any

import httpx
from fastapi import FastAPI


class _NoCookiePersistence(DefaultCookiePolicy):
    """Keep pooled proxy connections without retaining an upstream session."""

    def set_ok(self, cookie: Cookie, request: Any) -> bool:
        return False


@dataclass(slots=True)
class ProxyRuntime:
    """Group outbound resources shared by one application instance."""

    http_client: httpx.AsyncClient

    @classmethod
    def create(
        cls,
        *,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> "ProxyRuntime":
        return cls(
            http_client=httpx.AsyncClient(
                follow_redirects=False,
                trust_env=False,
                transport=http_transport,
                cookies=CookieJar(policy=_NoCookiePersistence()),
            )
        )

    async def close(self) -> None:
        await self.http_client.aclose()


def get_runtime(application: FastAPI) -> ProxyRuntime:
    """Return the runtime initialized by the application's active lifespan."""

    runtime = getattr(application.state, "experimental_proxy_runtime", None)
    if not isinstance(runtime, ProxyRuntime):
        raise RuntimeError("The experimental proxy runtime is not available.")
    return runtime
