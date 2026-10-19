import pytest


@pytest.fixture(autouse=True)
def _no_live_llm_judge(monkeypatch):
    """Never call the real Gemini judge from tests; tests that want it inject one."""
    monkeypatch.setenv("MYNEWS_LLM_EVALS", "off")
