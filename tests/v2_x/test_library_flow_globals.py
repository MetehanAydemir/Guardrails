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

"""Colang 2 library flows must declare shared globals before using them.

In Colang 2, a flow that reads ``$user_message`` without ``global $user_message``
reads a flow-local variable (``None``) and an assignment creates a local shadow.
The rail action then checks ``None`` and the rail silently passes.
"""

import re
from pathlib import Path

import pytest

from nemoguardrails import RailsConfig
from nemoguardrails.actions.rail_outcome import RailOutcome, TransformTarget
from tests.utils import TestChat

LIBRARY_DIR = Path(__file__).resolve().parents[2] / "nemoguardrails" / "library"

SHARED_GLOBALS = ("user_message", "bot_message", "relevant_chunks")

# Tracked separately in https://github.com/NVIDIA-NeMo/Guardrails/issues/2172.
KNOWN_EXCEPTIONS = {("hf_classifier/flows.co", "hf classifier check retrieval $classifier", "relevant_chunks")}

_STRING_RE = re.compile(r'"(?:\\.|[^"\\])*"' + r"|'(?:\\.|[^'\\])*'")
_INTERPOLATION_RE = re.compile(r"\{([^{}]+)\}")
_GLOBAL_RE = re.compile(r"global\s+\$(\w+)\s*$")


def _code_only(line: str) -> str:
    """Drop string literals but keep their ``{...}`` interpolated expressions."""
    return _STRING_RE.sub(lambda m: " ".join(_INTERPOLATION_RE.findall(m.group(0))), line)


def _iter_flows(text: str):
    """Yield ``(flow_name, [(lineno, indent, statement), ...])`` for each flow."""
    name = None
    statements = []
    in_docstring = False
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if line and not line[0].isspace():
            if name is not None:
                yield name, statements
            match = re.match(r"flow\s+(.+)", line)
            name = match.group(1).strip() if match else None
            statements = []
            in_docstring = False
            continue
        if name is None or not stripped or stripped.startswith("#"):
            continue
        if in_docstring:
            in_docstring = '"""' not in stripped
            continue
        if stripped.startswith('"""'):
            in_docstring = stripped.count('"""') == 1
            continue
        statements.append((lineno, len(line) - len(line.lstrip()), stripped))
    if name is not None:
        yield name, statements


def _undeclared_uses(path: Path, root: Path = LIBRARY_DIR):
    """Find uses of shared globals not preceded by a ``global`` in an enclosing block."""
    rel = path.relative_to(root).as_posix()
    problems = []
    for flow_name, statements in _iter_flows(path.read_text(encoding="utf-8")):
        declared = []  # (var, indent) of global statements whose block is still open
        reported = set()
        for lineno, indent, statement in statements:
            declared = [(var, level) for var, level in declared if indent >= level]
            match = _GLOBAL_RE.match(statement)
            if match:
                declared.append((match.group(1), indent))
                continue
            code = _code_only(statement)
            for var in SHARED_GLOBALS:
                if var in reported or (rel, flow_name, var) in KNOWN_EXCEPTIONS:
                    continue
                if re.search(rf"\${var}\b", code) and var not in {v for v, _ in declared}:
                    reported.add(var)
                    problems.append(f"{rel}:{lineno}: flow '{flow_name}' uses ${var} without 'global ${var}'")
    return problems


COLANG_2_FLOW_FILES = sorted(LIBRARY_DIR.rglob("flows.co"))


def test_colang_2_library_flow_files_found():
    assert len(COLANG_2_FLOW_FILES) > 20


@pytest.mark.parametrize(
    "path",
    COLANG_2_FLOW_FILES,
    ids=[p.relative_to(LIBRARY_DIR).as_posix() for p in COLANG_2_FLOW_FILES],
)
def test_library_flows_declare_shared_globals_before_use(path):
    assert _undeclared_uses(path) == []


def test_checker_flags_missing_and_late_global(tmp_path):
    flows = tmp_path / "flows.co"
    flows.write_text(
        "flow missing\n"
        '  """Uses $user_message in a docstring only."""\n'
        "  $r = await A(text=$user_message)\n"
        "\n"
        "flow late\n"
        "  $r = await A(text=$bot_message)\n"
        "  if $r.is_transform\n"
        "    global $bot_message\n"
        "    $bot_message = 'x'\n"
        "\n"
        "flow ok\n"
        "  global $relevant_chunks\n"
        '  bot say "{$relevant_chunks}"\n'
        "\n"
        "flow ok branch write\n"
        "  if $r.is_transform\n"
        "    global $user_message\n"
        "    $user_message = 'x'\n"
        "\n"
        "flow string literal only\n"
        '  bot say "no $bot_message interpolation here"\n',
        encoding="utf-8",
    )
    problems = _undeclared_uses(flows, root=tmp_path)
    assert problems == [
        "flows.co:3: flow 'missing' uses $user_message without 'global $user_message'",
        "flows.co:6: flow 'late' uses $bot_message without 'global $bot_message'",
    ]


# Behavioural checks: the rail action must receive the real message text.

USER_TEXT = "my ssn is 123-45-6789"
BOT_TEXT = "BOT OUTPUT"


def _regex_config(flow: str, direction: str) -> RailsConfig:
    return RailsConfig.from_content(
        yaml_content=f"""
            colang_version: 2.x
            models: []
            rails:
              config:
                regex_detection:
                  {direction}:
                    patterns:
                      - "\\\\d{{3}}-\\\\d{{2}}-\\\\d{{4}}"
        """,
        colang_content=f"""
            import core
            import llm
            import guardrails
            import nemoguardrails.library.regex

            flow {direction} rails ${direction}_text
              {flow}

            flow main
              activate llm continuation
              user said something
              bot say "{BOT_TEXT} 987-65-4321"
        """,
    )


def test_colang_2_regex_check_input_blocks_matching_user_message():
    chat = TestChat(_regex_config("regex check input", "input"))

    chat >> USER_TEXT
    chat << "I'm sorry, I can't respond to that."


def test_colang_2_regex_check_output_blocks_matching_bot_message():
    chat = TestChat(_regex_config("regex check output", "output"))

    chat >> "hello"
    chat << "I'm sorry, I can't respond to that."


def test_colang_2_regex_check_input_action_receives_user_message():
    chat = TestChat(_regex_config("regex check input", "input"))
    seen = []

    async def spy(source, text, **kwargs):
        seen.append((source, text))
        return RailOutcome.allow()

    chat.app.register_action(spy, "detect_regex_pattern")

    chat >> USER_TEXT
    chat << f"{BOT_TEXT} 987-65-4321"
    assert seen == [("input", USER_TEXT)]


def _injection_detection_config() -> RailsConfig:
    return RailsConfig.from_content(
        yaml_content="""
            colang_version: 2.x
            models: []
            enable_rails_exceptions: false
            rails:
              config:
                injection_detection:
                  action: omit
        """,
        colang_content=f"""
            import core
            import llm
            import guardrails
            import nemoguardrails.library.injection_detection

            flow output rails $output_text
              injection detection

            flow main
              activate llm continuation
              user said something
              bot say "{BOT_TEXT}"
        """,
    )


def test_colang_2_injection_detection_receives_and_transforms_bot_message():
    chat = TestChat(_injection_detection_config())
    seen = []

    async def spy(text, **kwargs):
        seen.append(text)
        return RailOutcome.transform(
            [(TransformTarget.BOT_MESSAGE, text.replace("OUTPUT", "SANITIZED"))],
            metadata={"is_injection": True},
        )

    chat.app.register_action(spy, "injection_detection")

    chat >> "hello"
    chat << "BOT SANITIZED"
    assert seen == [BOT_TEXT]
