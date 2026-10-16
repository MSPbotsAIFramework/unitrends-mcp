# Copyright 2026 MSPbots.ai
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Token-economical JSON serialization for tool results.

Compact (no indent), non-ASCII preserving, and capped at MAX_CHARS so one
tool call can never flood the agent's context.
"""

import json
from typing import Any

MAX_CHARS = 20_000


def compact(data: Any) -> str:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False, default=str)


def dump_json_capped(data: Any, max_chars: int = MAX_CHARS) -> str:
    """Serialize ``data``; if it is too large, shrink its biggest list field.

    The result then carries ``truncated``/``truncated_field``/``original_count``
    and ``has_more`` is forced to true so the agent knows to narrow the query
    or page further.
    """
    text = compact(data)
    if len(text) <= max_chars:
        return text

    if isinstance(data, list):
        data = {"items": data}

    if isinstance(data, dict):
        list_keys = [k for k, v in data.items() if isinstance(v, list)]
        if list_keys:
            key = max(list_keys, key=lambda k: len(compact(data[k])))
            items = data[key]

            def build(n: int) -> dict:
                candidate = dict(data)
                candidate[key] = items[:n]
                candidate["has_more"] = True
                candidate["truncated"] = True
                candidate["truncated_field"] = key
                candidate["original_count"] = len(items)
                candidate["note"] = (
                    "Result truncated to fit the response size limit; use a smaller "
                    "limit or narrower filters."
                )
                return candidate

            lo, hi = 0, len(items)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if len(compact(build(mid))) <= max_chars:
                    lo = mid
                else:
                    hi = mid - 1
            result = compact(build(lo))
            if len(result) <= max_chars:
                return result

    return compact(
        {
            "truncated": True,
            "note": (
                f"Result too large to return ({len(text)} chars); narrow the query "
                "with filters or a smaller limit."
            ),
        }
    )


def error_envelope(code: str, message: str, retryable: bool) -> str:
    return compact({"error": {"code": code, "message": message, "retryable": retryable}})
