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

"""Protect relationships in sanitized Responses recordings, not real provider evidence."""

import json

from tests.recorded.server.experimental.openai.responses_support import sanitize_responses_recording


def test_responses_sanitization_preserves_distinct_ids_and_references():
    payloads = [
        {"type": "response.created", "response": {"id": "private-routing-id", "created_at": 123}},
        {"type": "response.output_item.added", "item": {"id": "first-item"}},
        {"type": "response.output_text.delta", "item_id": "first-item", "delta": "original text", "sequence_number": 4},
        {"type": "response.output_item.done", "item": {"id": "other-item"}},
        {"type": "response.completed", "response": {"id": "private-routing-id", "completed_at": 456}},
    ]
    response = {
        "headers": {"content-type": ["text/event-stream"], "authorization": ["Bearer test-secret"]},
        "body": {"string": "".join(f"event: {p['type']}\ndata: {json.dumps(p)}\n\n" for p in payloads)},
    }
    sanitized = sanitize_responses_recording(response)
    text = sanitized["body"]["string"]
    events = [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]
    assert events[0]["response"]["id"] == events[4]["response"]["id"]
    assert events[1]["item"]["id"] == events[2]["item_id"]
    assert events[1]["item"]["id"] != events[3]["item"]["id"]
    assert events[2]["delta"] == "original text"
    assert events[2]["sequence_number"] == 4
    assert events[0]["response"]["created_at"] == events[4]["response"]["completed_at"] == 0
    assert [line for line in text.splitlines() if line.startswith("event:")] == [
        f"event: {p['type']}" for p in payloads
    ]
    assert not any(value in text for value in ("private-routing-id", "first-item", "other-item"))
    assert "authorization" not in sanitized["headers"]
    assert "private-routing-id" in response["body"]["string"]


def test_responses_sanitization_preserves_unknown_fields_and_disabled_content():
    body = {
        "id": "routing-id",
        "created_at": 123,
        "output": [{"id": "item", "type": "message", "encrypted_content": "opaque-provider-state"}],
        "new_field": {"text": "do not hide"},
        "api_key": "secret",
    }
    sanitized = sanitize_responses_recording(
        {"headers": {"content-type": ["application/json"]}, "body": {"string": json.dumps(body).encode()}}
    )
    payload = json.loads(sanitized["body"]["string"])
    assert payload["new_field"] == body["new_field"]
    assert payload["output"][0]["type"] == "message"
    assert payload["api_key"] == "[REDACTED]"
    assert payload["output"][0]["id"] != "item"
    assert payload["output"][0]["encrypted_content"] == "[REDACTED_ENCRYPTED_CONTENT]"
