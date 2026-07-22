"""Compile-step helpers: prompt building and JSON extraction. The Gemini call
itself is not exercised here (no network); compile_interests is covered by
mocking in integration, but its pure parts are tested directly."""

from __future__ import annotations

import pytest

from news_bot.compile import _extract_json, build_compile_prompt


def test_prompt_embeds_interests_and_schema():
    prompt = build_compile_prompt("  entertainment industry news  ")
    assert "entertainment industry news" in prompt
    assert '"persona"' in prompt
    assert '"sections"' in prompt
    assert "6 to 9 sections" in prompt


def test_extract_plain_json():
    obj = _extract_json('{"persona": "p", "edge_for": "e", "sections": []}')
    assert obj["persona"] == "p"


def test_extract_json_from_code_fence():
    raw = '```json\n{"persona": "p", "sections": [1]}\n```'
    assert _extract_json(raw)["persona"] == "p"


def test_extract_json_with_stray_prose():
    raw = 'Here is your config:\n{"persona": "p", "sections": [1]}\nEnjoy!'
    assert _extract_json(raw)["persona"] == "p"


def test_extract_invalid_json_raises():
    with pytest.raises(ValueError):
        _extract_json("not json at all")
