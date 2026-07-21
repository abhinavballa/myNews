"""compiled_profile schema validation — malformed compiler output must be
rejected at save time, not at 8am."""

from __future__ import annotations

import pytest

from news_bot.profiles import CompiledProfileError, validate_compiled_profile

VALID = {
    "persona": "an entertainment industry follower",
    "edge_for": "what to watch, plus industry and culture angles",
    "sections": [
        {"emoji": "🎬", "title": "Box Office & Releases", "guidance": "3-4 bullets."},
    ],
}


def test_valid_profile_passes():
    validate_compiled_profile(VALID)  # must not raise


@pytest.mark.parametrize("bad", [
    None,
    [],
    "a string",
    {**VALID, "persona": ""},
    {**VALID, "persona": "   "},
    {k: v for k, v in VALID.items() if k != "edge_for"},
    {**VALID, "sections": []},
    {**VALID, "sections": "not a list"},
    {**VALID, "sections": [{"emoji": "🎬", "title": "T"}]},          # missing guidance
    {**VALID, "sections": [{"emoji": "", "title": "T", "guidance": "g"}]},
    {**VALID, "sections": ["not an object"]},
])
def test_malformed_profiles_rejected(bad):
    with pytest.raises(CompiledProfileError):
        validate_compiled_profile(bad)
