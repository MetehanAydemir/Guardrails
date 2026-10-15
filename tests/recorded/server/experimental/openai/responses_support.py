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

"""Sanitize Responses evidence without merging distinct stream item identities."""

import json
from copy import deepcopy

from tests.recorded.conftest import before_record_response


def sanitize_responses_recording(response):
    """Remove routing identifiers while retaining identity equality and event order.

    One mapping spans the complete HTTP body, including all SSE events. Unlike
    replacing every id with one sentinel, this preserves mismatches and distinct
    items. Text, sequence numbers, output indices and extension fields survive.
    The shared sanitizer still owns credential and HTTP header redaction.
    """
    response = deepcopy(response)
    identities = {}

    def scrub(value):
        if isinstance(value, list):
            return [scrub(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {}
        for key, nested in value.items():
            if key in {"id", "item_id", "response_id"} and isinstance(nested, str):
                result[key] = identities.setdefault(nested, f"recorded_id_{len(identities)}")
            elif key in {"created_at", "completed_at"} and nested is not None:
                result[key] = 0
            elif key == "encrypted_content" and isinstance(nested, str):
                result[key] = "[REDACTED_ENCRYPTED_CONTENT]"
            else:
                result[key] = scrub(nested)
        return result

    body = response.get("body", {}).get("string")
    if isinstance(body, (str, bytes)):
        text = body.decode() if isinstance(body, bytes) else body
        content_type = next(
            (value for name, value in response.get("headers", {}).items() if name.lower() == "content-type"), []
        )
        if isinstance(content_type, str):
            content_type = [content_type]
        if any("text/event-stream" in value.lower() for value in content_type):
            lines = []
            for line in text.splitlines(keepends=True):
                if line.startswith("data: ") and line.strip() != "data: [DONE]":
                    try:
                        payload = json.loads(line[6:])
                    except json.JSONDecodeError:
                        pass
                    else:
                        ending = line[len(line.rstrip("\r\n")) :]
                        line = "data: " + json.dumps(scrub(payload), separators=(",", ":")) + ending
                lines.append(line)
            text = "".join(lines)
        else:
            try:
                text = json.dumps(scrub(json.loads(text)))
            except json.JSONDecodeError:
                pass
        response["body"]["string"] = text.encode() if isinstance(body, bytes) else text
    return before_record_response(response)
