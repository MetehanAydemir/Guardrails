# Guardrails configuration resolution

The private experimental proxy supports deployment-defined Guardrails policy
resolution without allowing callers to select an arbitrary configuration.
Applications declare logical config IDs during assembly and may inject a
resolver that selects one of those IDs for each guarded request.

This mechanism does not add native server configuration discovery. Config
locations and resolution policy belong to the embedding application.

## Config entries and resolution

`ConfigEntry` maps a deployment-owned logical ID to a configuration directory:

```python
from pathlib import Path

from nemoguardrails.server.experimental.rails import ConfigEntry

configs = (
    ConfigEntry("standard", Path("/configs/standard")),
    ConfigEntry("restricted", Path("/configs/restricted")),
)
```

The resolver returns a declared ID, never a path:

```python
from nemoguardrails.server.experimental.rails import ConfigContext


def resolve_config(context: ConfigContext) -> str:
    subject = context.request.state.guardrails_subject
    if "restricted-users@example.com" in subject.distribution_lists:
        return "restricted"
    return "standard"
```

The catalog and resolver are supplied when the private application is
assembled:

```python
from nemoguardrails.server.experimental._application import create_proxy_application

application = create_proxy_application(
    providers=providers,
    rails_configs=configs,
    config_resolver=resolve_config,
)
```

The runtime verifies that every selected ID belongs to the catalog. Resolver
failures, empty IDs, and unknown IDs fail closed with the provider-native
`config_resolution_failed` response before an upstream request is made. A
single declared config gets a static resolver automatically.

## Resolution context

The resolver receives a `ConfigContext` containing:

| Value | Meaning |
| --- | --- |
| `request` | The Starlette request for the guarded provider call. |
| `provider_type` | The provider protocol or integration type. |
| `provider_instance_id` | The deployment-local provider instance. |
| `operation_name` | The guarded operation, such as `chat_completions.create`. |

The provider and operation values are supplied by the server. Deployments do
not need to reconstruct them from paths. The request provides headers,
cookies, authentication state, and application middleware state, but raw
caller values are not trusted merely because they are available to the
resolver.

Middleware can attach normalized deployment-owned policy facts before a
guarded route runs:

```python
@application.middleware("http")
async def add_policy_subject(request, call_next):
    identity = await authenticate_request(request)
    request.state.guardrails_subject = PolicySubject(
        tenant_id=identity.tenant_id,
        application_id=identity.application_id,
        distribution_lists=frozenset(identity.distribution_lists),
    )
    return await call_next(request)
```

The resolver may be synchronous or asynchronous. It may consult trusted
application state, an authorization service, a tenant registry, a session
store, or rollout policy, but its result must still be one of the IDs declared
at application construction.

## Lifecycle

Config entries are validated and fixed during application assembly. Guardrails
cores are loaded lazily on first selection, cached by config ID, and closed at
application shutdown. Concurrent requests for one ID share one startup task;
different IDs receive independent cores. Startup failures are cached and
remain fail-closed.

The application runtime closes the checker repository and the shared outbound
HTTP client. HTTP cleanup still runs if checker shutdown fails.

The fixed catalog is an explicit allow-list and resource-ownership boundary.
It does not define hot reload, cache invalidation, concurrent replacement, or a
configuration CRUD control plane. Adding those capabilities later requires
separate decisions about authentication, persistence, versioning, atomicity,
rollback, auditing, and in-flight requests.

## Trust boundary

- Do not resolve filesystem paths from request headers, query parameters, or
  provider payload fields.
- Do not treat `x-nemo-guardrails-config` or a similar caller header as trusted
  policy selection. Such a header remains ordinary provider traffic unless
  trusted infrastructure authenticates and replaces it before resolution.
- Prefer authenticated `request.user`, `request.auth`, or deliberately written
  `request.state` values over raw caller metadata.
- Avoid storing or logging secrets that policy selection does not require.
- Keep model routing and Rails policy resolution independent: one selects the
  provider destination, while the other selects a declared Guardrails policy.
