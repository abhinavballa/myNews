"""compile_prompt builds a stable prompt from a compiled_profile — no network."""

from __future__ import annotations

from news_bot.generate import compile_prompt

PROFILE = {
    "persona": "an entertainment industry follower",
    "edge_for": "what to watch, plus industry and culture angles",
    "sections": [
        {"emoji": "🎬", "title": "Box Office & Releases", "guidance": "3-4 bullets on releases."},
        {"emoji": "📺", "title": "Streaming Wars", "guidance": "A table of platforms."},
    ],
}


def test_prompt_includes_persona_and_edge():
    prompt = compile_prompt(PROFILE, "Monday, July 21, 2026")
    assert "an entertainment industry follower" in prompt
    assert "what to watch, plus industry and culture angles" in prompt
    assert "Monday, July 21, 2026" in prompt


def test_prompt_renders_each_section_in_order():
    prompt = compile_prompt(PROFILE, "Monday, July 21, 2026")
    assert "<h2>🎬 Box Office & Releases</h2>" in prompt
    assert "<h2>📺 Streaming Wars</h2>" in prompt
    assert prompt.index("Box Office") < prompt.index("Streaming Wars")
    assert "3-4 bullets on releases." in prompt


def test_prompt_keeps_grounding_and_actionable_rules():
    prompt = compile_prompt(PROFILE, "Monday, July 21, 2026")
    assert "Google Search" in prompt
    assert "→ Edge:" in prompt
    assert "not financial advice" in prompt
