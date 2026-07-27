"""Transient-error classification and model fallback for Gemini calls."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from news_bot.generate import (
    _is_transient,
    generate_content_resilient,
    model_chain,
)


class FakeError(Exception):
    def __init__(self, msg="", code=None, status=None):
        super().__init__(msg)
        self.code = code
        self.status = status


# --- classification -------------------------------------------------------

def test_503_code_is_transient():
    assert _is_transient(FakeError(code=503))


def test_429_code_is_transient():
    assert _is_transient(FakeError(code=429))


def test_status_string_is_transient():
    assert _is_transient(FakeError(status="UNAVAILABLE"))


def test_message_only_503_is_transient():
    assert _is_transient(Exception("503 UNAVAILABLE. model experiencing high demand"))


def test_client_error_is_not_transient():
    assert not _is_transient(FakeError(code=400))
    assert not _is_transient(ValueError("bad schema"))


# --- fallback loop --------------------------------------------------------

class FakeClient:
    """Scripted generate_content: each item is either an exception to raise or a
    response to return, consumed in order. Records the models it was called with."""
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    @property
    def models(self):
        return self

    def generate_content(self, model, contents, config):
        self.calls.append(model)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_retries_same_model_then_falls_back():
    ok = SimpleNamespace(text="brief")
    client = FakeClient([FakeError(code=503), FakeError(code=503), ok])
    out = generate_content_resilient(
        client, "p", None, models=["flash", "pro"],
        attempts_per_model=2, sleep_fn=lambda s: None,
    )
    assert out is ok
    assert client.calls == ["flash", "flash", "pro"]  # 2x primary, then fallback


def test_succeeds_on_retry_without_fallback():
    ok = SimpleNamespace(text="brief")
    client = FakeClient([FakeError(code=503), ok])
    out = generate_content_resilient(
        client, "p", None, models=["flash", "pro"],
        attempts_per_model=2, sleep_fn=lambda s: None,
    )
    assert out is ok
    assert client.calls == ["flash", "flash"]


def test_non_transient_raises_immediately():
    client = FakeClient([FakeError(code=400)])
    with pytest.raises(FakeError):
        generate_content_resilient(
            client, "p", None, models=["flash", "pro"],
            attempts_per_model=2, sleep_fn=lambda s: None,
        )
    assert client.calls == ["flash"]  # did not retry or fall back


def test_raises_last_error_when_all_exhausted():
    client = FakeClient([FakeError(code=503)] * 4)
    with pytest.raises(FakeError):
        generate_content_resilient(
            client, "p", None, models=["flash", "pro"],
            attempts_per_model=2, sleep_fn=lambda s: None,
        )
    assert client.calls == ["flash", "flash", "pro", "pro"]


def test_model_chain_dedups_and_orders(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.5-flash")
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", "gemini-3.5-pro, gemini-3.5-flash")
    assert model_chain() == ["gemini-3.5-flash", "gemini-3.5-pro"]
