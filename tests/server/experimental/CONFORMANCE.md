# Offline provider conformance

This suite checks that the bound proxy implements its reviewed guard boundary
without accidentally changing provider traffic. It compares three distinct
things: an independently pinned provider specification, authoritative typed
Python declarations, and observable HTTP behavior. Export consistency is an
additional check, not the source from which runtime expectations are generated.

The initial source audit covers OpenAI Chat Completions. Existing LiteLLM tests
cover that explicitly selected dialect's extensions; the OpenAI source pin is
not evidence for every compatible deployment. Responses and Anthropic need
their own source inventories and protocol-specific expectations when integrated.

## What these tests establish

- Every property of each inspected source object has an explicit declaration
  or reviewed opaque policy. Local extensions are accounted for separately.
  A new source member or a newly bound nested model requires review.
- Provider-valid examples outside the guard profile are rejected explicitly.
  The proxy is not required to support every provider-valid request or response.
- Explicit synthetic examples exercise every reviewed root metadata field and
  every disabled root request field. This is field coverage, not exhaustive
  value/combination coverage.
- Raw request bytes are observed after the real HTTP dispatcher constructs the
  upstream request. Response and SSE bytes are compared to independently supplied
  bytes, including whitespace, Unicode, CRLF framing, and fragmented reads.
- Unknown response content is rejected when output inspection is enabled; the
  intentionally uninspected response path preserves it without safety claims.
- Disabling inspection does not remove guarded-request validation. A passthrough
  route is distinct from an uninspected request on the guarded operation.

The guard models are partial policy projections, not full provider validators.
For example, opaque values are forwarded without enforcing every upstream
constraint. The suite checks representative source-valid exchanges and explicit
restrictions; it does not prove that every payload accepted by the proxy will
be accepted by the provider.

## Coverage matrix and existing evidence

Paths in this table are relative to this directory unless linked otherwise.
The existing tests remain the owners of common kernel behavior; conformance
adds independent source evidence and actual-boundary assertions, not copies.

| Requirement | Evidence | Scope/limitation |
| --- | --- | --- |
| Source provenance, operation inventory, inspected-object field review | `test_openai_chat_schema_conformance.py` | Pinned document; no automatic upstream-change discovery. |
| Extraction integrity and nullable interpretation | Same schema suite; `fixtures/openai_chat/extract_source.py` | Regression tests preserve named fields and literal values; extraction verifies the source digest. |
| Reviewed metadata preservation and exact text extraction | `test_openai_chat_http_conformance.py` | Synthetic source-valid payloads, actual router/HTTP dispatcher, mocked network boundary. |
| Provider-valid unsupported requests and content | Same HTTP suite | Explicit disabled/constrained cases; compatible errors and withheld unsupported output. |
| Unknown fields and inspection-disabled behavior | Same HTTP suite; `test_openai_chat_projections.py` | Default closed policy; trusted configurable-object overrides and case aliases remain projection tests. |
| Allowed/blocked decisions and inspection ordering | `test_provider_transport.py`; [recorded Chat tests](../../recorded/server/experimental/openai/test_chat_completions.py) | Checker doubles; input blocks before dispatch, output blocks withhold content. |
| First/later windows and overlapping context | Recorded Chat tests | Previously approved windows cannot be retracted; exact checked inputs and released prefix are asserted. |
| Stream roles, termination, errors, fresh state, truncation | `test_openai_chat_streaming_projections.py`, `test_openai_chat_streaming_integration.py`, `test_streaming_http.py` | Real classifier/hooks with synthetic events; not a live socket-delivery claim. |
| Export follows bound declarations | `test_openai_chat_contract_export.py`, `test_stream_contract_export.py`, schema conformance inventory checks | Runtime-only strictness and handwritten hook behavior are not completely expressible in JSON Schema. |
| LiteLLM extension boundaries and observed exchanges | `test_litellm_chat_dialect.py`; [recorded inventory](../../recorded/server/experimental/openai/test_litellm_inventory.py) | Reviewed source-backed cases and finite recordings, not universal backend compatibility. |
| HTTP framing, credential isolation, cancellation, response ownership | `test_provider_transport.py`, `test_httpx_response_cleanup.py`, `test_streaming_http.py` | Existing unit/integration evidence; does not close the separate shared-client shutdown issue. |
| Real Rails configurations, localhost incremental delivery, disconnects and application lifecycle | Later application/example work | Not established by these checker-double, in-process tests. Keep lifecycle prerequisites separately tracked. |

## Intentional routing limitations

The pinned source lists six Chat operations. The selected router guards only
`POST /v1/chat/completions`. Other methods on that same reserved path, including
the provider's documented list operation, return 405 rather than falling through.
The four documented item operations on `/v1/chat/completions/{id}` and its
`/messages` child are relayed without guardrails. Their tests establish transport
preservation, not validation of those operations' full provider schemas.

## Source fidelity

The [fixture](fixtures/openai_chat/README.md) is an independently extracted public
specification, not the exported guard contract. The extractor omits documentation
annotations but preserves property names such as `description`, schema names,
and literal default/constant values. Tests pin the resulting fixture digest.

This upstream revision advertises OpenAPI 3.1 while retaining legacy `nullable`
annotations, including beside enums and references. `openai_conformance.py`
explicitly interprets those annotations for validation. It does not rewrite the
fixture, alter runtime declarations, or normalize payloads used in byte checks.
If source semantics change, review this interpretation rather than silently
updating snapshots. The lightweight validator is limited to the local component
references and schema constructs used by this pin; it is not a general OpenAPI
validation framework or a complete semantic compatibility proof.

## Adding another operation or dialect

1. Obtain an independently identified specification or reviewed protocol source.
   Record provenance and distinguish source-backed synthetic cases from recordings.
2. Audit the models reachable from the actual bound endpoint/classifier against
   the relevant inspected objects. Keep intentionally opaque subtrees opaque;
   do not require field-by-field declarations inside them.
3. Supply explicit valid examples and unsupported cases, with independent expected
   outcomes. Use source validation to establish why a rejected example is a guard
   restriction rather than simply invalid provider input.
4. Reuse `checkers.StaticChecker` and scenario intent. Keep event sequencing,
   terminal markers, error envelopes, and source-schema interpretation specific
   to the protocol. Do not reuse Chat's choice paths or `[DONE]` for Anthropic.
5. Extend the matrix and reuse existing kernel coverage. Extract shared harness
   code only when a second operation demonstrates the same needs; no compiler,
   registry, generic example generator, or conformance DSL is required.

Offline tests protect against regressions in our implementation relative to
reviewed assumptions. Separately authorized checks against real providers are
needed to detect whether those assumptions and recordings have become stale.
Neither a static schema pin nor an offline green suite supplies that evidence.

## Validation

```bash
make test WORKERS=0 \
  TEST='tests/server/experimental/test_openai_chat_schema_conformance.py tests/server/experimental/test_openai_chat_http_conformance.py' \
  ARGS='--block-network -q'
```

For the surrounding kernel, export, dialect, transport, and recorded evidence:

```bash
make test WORKERS=0 \
  TEST='tests/server/experimental tests/recorded/server/experimental' \
  ARGS='--block-network -q'
```

The tests use synthetic local transports and existing recorded exchanges only.
They do not record provider traffic, require credentials, or repair lifecycle
defects. Keep unverified guarantees visible instead of hiding them behind skips.
