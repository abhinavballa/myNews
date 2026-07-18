"""Generate the daily digest HTML using Gemini with Google Search grounding."""

from __future__ import annotations

import datetime
import os
import re

from google import genai
from google.genai import types

MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")

PROMPT = """You are a sharp research analyst AND opportunity scout writing a daily \
morning briefing for an ambitious AI engineer who wants to stay deeply knowledgeable in \
AI and geopolitics — AND to convert that knowledge into money, businesses, AI \
automations to build, and well-timed market bets. Today is {date} (Pacific time).

Use Google Search to gather REAL, UP-TO-DATE information from roughly the last 24-48 \
hours. Do NOT invent, guess, or use stale knowledge. Every item must reflect something \
that actually happened recently. Prefer primary/reputable sources. Include real \
numbers (prices, %, dates) where relevant.

Produce ONLY an HTML fragment (no <html>, <head>, or <body> tags, no markdown code \
fences). Use the EXACT section structure below. Each section is an <h2> followed by \
either a <ul> of concise bullets or an HTML <table>. Bold the key entity in each \
bullet using <strong>. Keep it tight and scannable — aim for about a 4-5 minute read.

CRITICAL RULE — every bullet must be ACTIONABLE, not just informative. Write each bullet \
as TWO parts:
1. THE FACT: one sentence on what actually happened (with real names/numbers).
2. A "<strong>→ Edge:</strong>" clause (one or two sentences) that draws the conclusion: \
what this could mean for the future of the world / the industry, AND specifically how the \
reader could leverage it. Make the Edge concrete — pick the most relevant angle for that \
item: a specific AI automation or product they could build, a new money-making or business \
opportunity, or a directional market read (a named stock/asset and a potential high or low \
to position for, framed as a speculative hypothesis — never as guaranteed advice). Avoid \
generic filler like "worth watching"; give a real, specific move.

Structure:

<h2>🤖 AI Innovation</h2>
<ul> 3-5 bullets on the biggest AI product / model / company moves in the last 24-48h, \
each with its → Edge clause </ul>

<h2>📄 Research & New Papers</h2>
<ul> 3-4 bullets on notable new papers or research (arXiv, labs). Fact + → Edge, where \
the Edge names what could be built or automated on top of this research </ul>

<h2>🏛️ Politics</h2>
<ul> 3-4 bullets on key US political / policy developments, each with its → Edge clause \
(policy shifts → who wins/loses, what to build, what to trade) </ul>

<h2>🌍 Geopolitics</h2>
<ul> 3-4 bullets on global power dynamics, conflicts, trade, or diplomacy, each with its \
→ Edge clause </ul>

<h2>📈 Tech Stocks Watch</h2>
Give an HTML table with columns: Ticker, Company, Move, Catalyst, Angle. Include 5-6 \
notable tech names with their most recent daily move (e.g. +2.3%), a short catalyst, and \
an "Angle" cell with a concrete speculative read (e.g. bullish/bearish setup, level to \
watch, or the second-order beneficiary). Use real, recent figures.

<h2>👀 Stocks to Watch</h2>
<ul> 2-3 bullets, each a ticker/company with a directional thesis: the catalyst, a \
potential high or low to position for this week, and why now. Mark as speculative. </ul>

<h2>🎯 Your Edge — Highest-Conviction Moves</h2>
<ul> 2-3 bullets synthesizing the single best opportunities from everything above: the \
strongest AI-automation or product idea to build right now, the best money/business \
angle, and the highest-conviction market setup (ticker + direction + why now). Each \
bullet must end with a concrete next action the reader could take this week. Frame all \
market calls as speculative hypotheses, not guaranteed advice. </ul>

<h2>💡 One Thing to Know</h2>
<p> One short paragraph teaching a useful AI concept, term, or technique to deepen the \
reader's fluency in the field. Make it genuinely educational. </p>

<p style="font-size:12px;color:#888;margin-top:16px">Market reads above are speculative \
analysis for idea generation, not financial advice. Do your own research.</p>

Return only the HTML fragment.
"""


def _clean_fragment(text: str) -> str:
    """Strip markdown code fences and stray html/body wrappers if present."""
    text = text.strip()
    # Remove ```html ... ``` fences.
    text = re.sub(r"^```(?:html)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    # Drop any full-document wrappers the model may add.
    for tag in ("html", "head", "body"):
        text = re.sub(rf"</?{tag}[^>]*>", "", text, flags=re.IGNORECASE)
    return text.strip()


def generate_digest_html() -> str:
    """Call Gemini with search grounding and return an HTML fragment for the email."""
    api_key = os.environ["GEMINI_API_KEY"]
    client = genai.Client(api_key=api_key)

    today = datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=-8))
    ).strftime("%A, %B %d, %Y")
    prompt = PROMPT.format(date=today)

    response = client.models.generate_content(
        model=MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            temperature=0.4,
        ),
    )

    fragment = _clean_fragment(response.text or "")
    if len(fragment) < 200:
        raise RuntimeError(
            f"Gemini returned too little content ({len(fragment)} chars): {fragment!r}"
        )
    return fragment
