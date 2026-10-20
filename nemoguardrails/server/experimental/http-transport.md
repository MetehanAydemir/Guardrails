# Outbound HTTP transport extension

The private staged application assembly accepts an optional HTTPX transport
factory. This is a low-level extension point for changing how provider requests
are dispatched without adding routing, networking, or deployment policy to the
Guardrails pipeline.

```python
import httpx

from nemoguardrails.server.experimental._application import create_proxy_application

application = create_proxy_application(
    providers=providers,
    checker=checker,
    http_transport_factory=lambda: httpx.MockTransport(handler),
)
```

The leading underscores are intentional while this stack is staged. This work
restores the exploratory capability without committing to a public provider
extension API. Trusted Rails configuration resolution replaces the single
supplied checker in the next PR; it does not change transport ownership or
routing semantics.

When no factory is supplied, HTTPX uses its normal asynchronous network
transport. When one is supplied, the application invokes it once per lifespan.
Every provider instance uses the resulting shared client. The runtime closes
the client and transport during shutdown. If startup fails after transport
creation but before runtime ownership succeeds, the lifespan closes the
transport while propagating the error.

HTTP client shutdown and startup rollback use a shielded five-second
cooperative cleanup budget. Timeouts and ordinary close failures are logged
without masking the original failure. Blocking or cancellation-suppressing
custom transports cannot be forcibly stopped by this budget.

## Position in the request flow

| Component | Responsibility |
| --- | --- |
| `ProviderIntegration` | Declares guarded endpoints, projections, renderers, an upstream, and provider-specific proxy control headers. |
| `UpstreamSettings` | Defines one provider instance's local mount and initial base URL. |
| `ProviderRoutePlan` | Shares mounted and upstream operation ownership between startup validation and request classification. |
| `ProviderTransport` | Builds provider requests, applies guarded representation and response-buffer rules, and relays buffered or streaming responses. |
| Pass-through router | Checks route ownership before reading request bytes, adapts unbuffered HTTP exchanges to ASGI responses, and owns cleanup through disconnect or completion. |
| HTTPX transport | Performs or replaces the final HTTPX dispatch operation. |

The provider transport strips hop-by-hop headers and the integration's declared
`control_headers` before requests reach the upstream. Provider authentication
and API-version headers are not control headers and continue upstream normally.

The shared client pools connections, not caller sessions. It does not persist
upstream cookies, and outbound requests do not inherit client-level cookies,
default headers, or authentication. Explicit caller `Cookie` and upstream
`Set-Cookie` headers still pass through normally. Trusted credential injection
belongs in the application transport, not in shared client defaults. Expected
network failures before response handoff use the provider-native error mapping;
cancellation and programming errors are not rewritten as network failures.

## Connection-pool capacity

The default pool is shared across provider instances within one application
lifespan. Each server worker has its own runtime and pool. Without an explicit
override, HTTPX's defaults apply: 100 total connections, 20 idle keep-alive
connections, and a five-second keep-alive expiry.

The default outbound transport uses HTTP/1.1. An active streaming response holds
a connection until its body is consumed or closed. A busy provider can therefore
use the available connections and make requests to another provider wait. Pool
acquisition uses the application's configured request `timeout`; exhaustion
surfaces through the existing upstream-failure mapping.

Configure the shared pool through the existing `http_transport_factory`, for example:

```python
application = create_proxy_application(
    providers=providers,
    checker=checker,
    http_transport_factory=lambda: httpx.AsyncHTTPTransport(
        limits=httpx.Limits(
            max_connections=200,
            max_keepalive_connections=40,
            keepalive_expiry=5.0,
        ),
        trust_env=False,
    ),
)
```

These are aggregate connection limits, not per-provider quotas. Size them for
expected concurrent streams and upstream capacity. Raising the limit does not
provide fairness or isolate providers from each other's traffic.

Custom transports own their pool configuration. The factory creates a fresh
transport for each application lifespan; the runtime owns and closes it.

## Local application routes

Declare health, readiness, or other local HTTP endpoints on routers and pass
them to the application factory before provider catch-alls are installed:

```python
from fastapi import APIRouter

local = APIRouter()

@local.get("/health")
async def health():
    return {"status": "ok"}

application = create_proxy_application(
    providers=providers,
    checker=checker,
    local_routers=(local,),
)
```

Local routes run before provider routes, and their paths are reserved from
pass-through forwarding for unsupported methods. For example, `GET /health`
runs the local handler and `POST /health` returns 405 without contacting a
provider. Nested routers and their prefixes and dependencies are preserved.
Choose local paths outside the provider's guarded operation paths.

Finish declaring local routes before application construction. Endpoints added
after construction, including with `@application.get(...)`, can be hidden by an
existing provider catch-all and are absent from its route reservations.

## Provider routing and forwarding

`provider_type` identifies a protocol family. Each provider instance explicitly
selects its own reviewed endpoints, so strict OpenAI and the LiteLLM dialect can
coexist on different mounts. `create_openai_chat_integration(endpoint=...)`
selects a dialect without changing other instances. Buffered-only endpoints do
not receive streaming bindings and reject streaming requests.

The configured request and response body limits apply to guarded traffic whose
JSON or SSE representation must be inspected. Pass-through request and response
bodies for unrelated provider operations stream without those guarded limits.
For unchanged streamed requests, one valid `Content-Length` is retained;
duplicate, malformed, or conflicting length headers are rejected. Bodyless
HTTP/1 requests are sent without chunked framing. HTTP/2 and HTTP/3 requests
without length headers continue streaming, since their bodies use protocol
frames. Guarded request lengths are recalculated from the outgoing bytes.
The pass-through router uses the instance's shared ownership policy to reject
local and mapped upstream aliases before opening an upstream request. Declared
canonical pass-through methods remain available. Outer ASGI mounts are removed
from decoded and raw paths together, preserving encoded suffixes and queries.
All provider routers are validated before they are installed.

Only guarded operations with output inspection request identity encoding;
pass-through traffic and uninspected guarded output preserve the caller's
`Accept-Encoding` value.

For guarded requests, the extension point is reached after input inspection
and before output inspection:

```text
caller
  -> guarded endpoint
  -> input inspection
  -> ProviderTransport
  -> HTTPX transport
  -> selected upstream
  -> HTTPX transport
  -> output inspection
  -> caller
```

It is not FastAPI middleware. FastAPI middleware surrounds requests entering
the ASGI application and responses leaving it; an HTTPX transport surrounds
the nested provider exchange initiated by the application.

## Relationship to `UpstreamSettings`

`UpstreamSettings.base_url` is fixed for one provider instance during the
application lifetime. It supplies the initial URL used for each outbound
request. The HTTPX transport receives that complete request and may send it
unchanged, route it to a trusted configured origin, or handle it without
network I/O.

If routing runs as a separate gateway service, use its reachable URL as the
fixed provider base URL. No custom transport is necessary for that setup.

## Other uses

The transport extension can support:

- deterministic provider tests with `httpx.MockTransport`;
- in-process ASGI composition with `httpx.ASGITransport` where its buffering is acceptable;
- model aliases, deployment-name translation, and model-to-origin routing;
- client-side load balancing, failover, or service discovery;
- controlled egress through a proxy, service mesh, or Unix domain socket;
- custom TLS and mutual-TLS configuration;
- traffic recording, replay, latency simulation, and fault injection;
- outbound request tracing and metrics when HTTPX event hooks are insufficient.

Use `UpstreamSettings` when only one provider instance's fixed base URL
changes. Use FastAPI middleware for inbound concerns such as request IDs and
authentication. Use provider contracts and projections for guardable payload
semantics.

## Constraints and ownership

- The transport is application-wide and sees requests for every configured provider.
- A multi-provider transport must dispatch using logical hosts, paths, or other server-controlled metadata.
- Provider catch-all routes stream request and response bodies without applying guarded-route buffer limits.
- The factory must return a new transport for each lifespan; the application runtime closes it.
- A production transport must preserve HTTPX streaming, cancellation, and response-close behavior.
- Dispatch owns each response until handoff; the returned response then owns cleanup, including failure before iteration. Cooperative cleanup attempts are shielded and bounded, and failures are logged without replacing the primary outcome. Closing must tolerate retries; a timeout does not establish that resources were freed.
- Response cleanup budgets are separate from transport read timeouts and application shutdown.
- `httpx.ASGITransport` buffers ASGI responses and is not the production choice for guarded streaming.
- Dynamic destinations must come from trusted configuration; arbitrary client-provided URLs would introduce SSRF risk.
- Cross-origin routing must apply the credential belonging to the selected origin.
- The transport must not apply Guardrails or interpret provider contracts.
