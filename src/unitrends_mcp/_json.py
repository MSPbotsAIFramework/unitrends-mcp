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
    """Serialize ``data`` within ``max_chars`` without ever skipping records.

    When the result is too large, the biggest list field is reduced in two steps:

    1. Long text and long nested lists inside its records are shortened, step by
       step. If every record then fits, all of them are returned (with
       ``fields_truncated``), so paging fields stay correct.
    2. Otherwise only the first records that fit are returned: ``count`` becomes
       the number returned (``original_count`` keeps the page size), and the
       next-page fields are removed, because continuing from them would skip the
       records that were cut. ``retry_page_size`` says how many records fit. A list
       helper whose API accepts any start offset can pass
       ``_resume: {"key": <next field>, "start": <offset of this page>}``; the next
       offset is then set to just after the last record returned.

    ``_resume`` is never part of the output.
    """
    resume = None
    if isinstance(data, dict) and _RESUME in data:
        resume = data[_RESUME]
        data = {k: v for k, v in data.items() if k != _RESUME}
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

            # Step 1: shorten long values inside the records, keeping every record.
            shortened = items
            for text_limit, list_limit in _SHRINK_STEPS:
                shortened = [_shrink(item, text_limit, list_limit) for item in items]
                candidate = dict(data)
                candidate[key] = shortened
                candidate["fields_truncated"] = True
                candidate["note"] = (
                    "Long fields were shortened to fit the response size limit; "
                    "use the matching get tool for complete records."
                )
                result = compact(candidate)
                if len(result) <= max_chars:
                    return result

            # Step 2: return only the records that fit, without a skipping continuation.
            def build(n: int) -> dict:
                candidate = {k: v for k, v in data.items() if k not in _CONTINUATION_KEYS}
                candidate[key] = shortened[:n]
                if "count" in data:
                    candidate["count"] = n
                candidate["original_count"] = len(items)
                candidate["has_more"] = True
                candidate["truncated"] = True
                candidate["truncated_field"] = key
                candidate["retry_page_size"] = max(n, 1)
                if resume and isinstance(resume.get("start"), int) and resume.get("key"):
                    candidate[resume["key"]] = resume["start"] + n
                    candidate["note"] = (
                        f"Only {n} of {len(items)} records fit the response size limit; "
                        f"continue from {resume['key']}, which starts right after the last record returned."
                    )
                else:
                    candidate["note"] = (
                        f"Only {n} of {len(items)} records fit the response size limit, so the "
                        f"next-page fields were removed (they would skip records). Call again with "
                        f"a page size of {max(n, 1)} and the same filters and starting point."
                    )
                return candidate

            lo, hi = 0, len(items)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if len(compact(build(mid))) <= max_chars:
                    lo = mid
                else:
                    hi = mid - 1
            if lo > 0:
                result = compact(build(lo))
                if len(result) <= max_chars:
                    return result

    return compact(
        {
            "truncated": True,
            "note": (
                f"Result too large to return ({len(text)} chars); narrow the query "
                "with filters or a smaller page size."
            ),
        }
    )


# Fields that point to the next page. When records are cut they would skip them.
_CONTINUATION_KEYS = frozenset(
    {
        "next_page", "next_offset", "next_skip", "next_cursor", "next_page_token", "next_token",
        "starting_after", "next", "next_link", "nextPageAfter", "next_start", "nextPage", "next_page_url",
    }
)
_RESUME = "_resume"
# (max characters per text value, max items per nested list), mildest first.
_SHRINK_STEPS = ((2000, 50), (500, 20), (200, 10), (80, 5))


def _shrink(value: Any, text_limit: int, list_limit: int) -> Any:
    if isinstance(value, str):
        return value if len(value) <= text_limit else value[:text_limit] + "...[truncated]"
    if isinstance(value, list):
        kept = [_shrink(v, text_limit, list_limit) for v in value[:list_limit]]
        return kept + ([f"...[{len(value) - list_limit} more]"] if len(value) > list_limit else [])
    if isinstance(value, dict):
        return {k: _shrink(v, text_limit, list_limit) for k, v in value.items()}
    return value


def error_envelope(code: str, message: str, retryable: bool) -> str:
    return compact({"error": {"code": code, "message": message, "retryable": retryable}})
