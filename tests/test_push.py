"""Push pruning decision and teaser building."""

from __future__ import annotations

from news_bot.generate import build_teaser
from news_bot.push import _should_prune


# --- prune decision -------------------------------------------------------

def test_prune_on_410():
    assert _should_prune(410)


def test_prune_on_404():
    assert _should_prune(404)


def test_no_prune_on_transient_or_ok():
    assert not _should_prune(201)
    assert not _should_prune(500)
    assert not _should_prune(None)


# --- teaser ---------------------------------------------------------------

def test_teaser_uses_first_bold_entity():
    fragment = (
        "<h2>🤖 AI</h2><ul>"
        "<li><strong>OpenAI</strong> shipped a thing. "
        "<strong>→ Edge:</strong> build on it.</li></ul>"
    )
    assert build_teaser(fragment) == "Today: OpenAI"


def test_teaser_skips_edge_marker_bold():
    fragment = "<li><strong>→ Edge:</strong> do something.</li>"
    # only bold is the Edge marker -> fall back
    assert build_teaser(fragment) == "Your morning brief is ready ☕"


def test_teaser_fallback_when_no_bold():
    assert build_teaser("<p>no bold here</p>") == "Your morning brief is ready ☕"


def test_teaser_is_capped():
    long_entity = "X" * 200
    fragment = f"<li><strong>{long_entity}</strong> happened.</li>"
    assert len(build_teaser(fragment)) <= 120
