# Pinned OpenAI Chat Completions specification

`pinned.openapi.yaml` is a structural extract of the OpenAI OpenAPI document
identified by `nemoguardrails/server/experimental/contracts/openai/source.yaml`.
It contains all six Chat Completions operations and their reachable schemas.
Documentation annotations are omitted so the test fixture stays small; actual
properties named `description`, `title`, or `examples` and literal schema values
are preserved. This is independent provider evidence, not an authored guard
contract or generated runtime policy.

Source: [OpenAI OpenAPI revision df63773](https://github.com/openai/openai-openapi/blob/df63773f69f542ef875b9f00c3837c25ba5f4f2a/openapi.yaml),
licensed MIT. Original source SHA-256:
`f2dae1a9aced09b91310db89edda51bf1e36ecbfb05230c3a50b239c07708469`.
Extract SHA-256:
`a84917786d64f8a04998da6f166b9b293eba20a1f093dd49ac69a1498f78454f`.

This pin contains legacy `nullable` annotations despite declaring OpenAPI 3.1.
The [conformance guide](../../CONFORMANCE.md) describes the explicit validation
interpretation and the limits of offline compatibility evidence.

To refresh it, download the exact `download_url` from the descriptor, verify
its SHA-256 digest, then run:

```bash
uv run --locked python tests/server/experimental/fixtures/openai_chat/extract_source.py \
  /path/to/openapi.yaml \
  nemoguardrails/server/experimental/contracts/openai/source.yaml \
  tests/server/experimental/fixtures/openai_chat/pinned.openapi.yaml
```

The extractor rejects source bytes that differ from the descriptor and rejects
non-component references. Update the fixture hashes here and in
`test_openai_chat_schema_conformance.py` only after reviewing an intentional
source or extraction change. Run the source audit and behavioral tests; new
fields must receive a policy decision rather than an automatic opaque allowance.
