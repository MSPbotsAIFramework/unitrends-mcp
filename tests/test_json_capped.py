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
"""Size-capped results never skip records: shorten fields first, then cut without a skipping continuation."""

import json

from unitrends_mcp._json import MAX_CHARS, dump_json_capped


def _load(text: str) -> dict:
    assert len(text) <= MAX_CHARS
    return json.loads(text)


def test_small_result_is_unchanged_and_resume_hint_is_hidden():
    data = {"items": [{"id": 1}], "count": 1, "next_page": 2, "_resume": {"key": "next_skip", "start": 0}}
    assert _load(dump_json_capped(data)) == {"items": [{"id": 1}], "count": 1, "next_page": 2}
    assert "_resume" in data  # the caller's dict is not modified


def test_long_fields_are_shortened_and_every_record_kept():
    rows = [{"id": i, "notes": "x" * 4800, "tags": list(range(200))} for i in range(25)]
    out = _load(dump_json_capped({"items": rows, "count": 25, "page": 1, "next_page": 2}))
    assert [r["id"] for r in out["items"]] == list(range(25))
    assert out["count"] == 25 and out["next_page"] == 2
    assert out["fields_truncated"] is True and "truncated" not in out


def test_cut_records_drop_the_continuation_and_fix_count():
    # Records with many small fields cannot be shortened enough.
    rows = [{"id": i, **{f"f{j}": f"value-{j:04d}" for j in range(120)}} for i in range(25)]
    data = {"items": rows, "count": 25, "page": 1, "has_more": True, "next_page": 2, "next_cursor": "abc"}
    out = _load(dump_json_capped(data))
    n = len(out["items"])
    assert 0 < n < 25
    assert out["count"] == n and out["original_count"] == 25 and out["retry_page_size"] == n
    assert out["has_more"] is True and out["truncated"] is True
    assert "next_page" not in out and "next_cursor" not in out
    assert [r["id"] for r in out["items"]] == list(range(n))


def test_resume_hint_continues_right_after_the_last_record():
    rows = [{"id": i, **{f"f{j}": f"value-{j:04d}" for j in range(120)}} for i in range(25)]
    data = {"items": rows, "count": 25, "next_skip": 75, "_resume": {"key": "next_skip", "start": 50}}
    out = _load(dump_json_capped(data))
    n = len(out["items"])
    assert out["next_skip"] == 50 + n
    assert "_resume" not in out


def test_nothing_fits_returns_a_note():
    out = _load(dump_json_capped({"items": [{"id": 1, **{f"f{j}": "v" * 9 for j in range(3000)}}], "next_page": 2}))
    assert out["truncated"] is True and "next_page" not in out



def test_single_oversized_record_is_shortened_not_dropped():
    # PRD-21055: one record larger than the cap (e.g. a long HTML description) keeps its fields.
    record = {"id": 7, "subject": "Printer down", "description": "<p>" + "x" * 30_000 + "</p>", "tags": ["a", "b"]}
    out = json.loads(dump_json_capped(record))
    assert (out["id"], out["subject"], out["tags"]) == (7, "Printer down", ["a", "b"])
    assert out["fields_truncated"] is True and out["description"].endswith("...[truncated]")
    assert len(json.dumps(out)) <= MAX_CHARS


def test_record_with_a_small_list_and_a_huge_field_is_shortened():
    record = {"id": 8, "body": "y" * 50_000, "conversations": [{"id": i, "text": "z" * 100} for i in range(3)]}
    out = json.loads(dump_json_capped(record))
    assert out["id"] == 8 and len(out["conversations"]) == 3 and out["fields_truncated"] is True



def test_one_record_too_large_on_its_own_is_returned_alone_and_continues():
    # A record whose nested objects alone exceed the cap: returned by itself, trimmed, and the
    # continuation points at the next record instead of the whole result being dropped.
    huge = {"id": "big", "configuration": {f"k{i}": {f"n{j}": "v" * 30 for j in range(30)} for i in range(400)}}
    data = {"items": [huge, {"id": "next"}], "count": 2, "has_more": True, "next_offset": 7,
            "_resume": {"key": "next_offset", "start": 5}}
    out = json.loads(dump_json_capped(data))
    assert [r["id"] for r in out["items"]] == ["big"] and out["fields_truncated"] is True
    assert out["next_offset"] == 6 and out["count"] == 1
    assert "more keys" in json.dumps(out["items"][0]) and len(json.dumps(out)) <= MAX_CHARS
