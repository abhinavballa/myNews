"""Generate the daily digest HTML using Gemini with Google Search grounding."""

from __future__ import annotations

import datetime
import os
import re

from google import genai
from google.genai import types

MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")

PROMPT = """You are a sharp research analyst writing a daily morning briefing for an \
ambitious engineer who works in AI and wants to stay deeply knowledgeable in AI \
and geopolitics. Today is {date} (Pacific time).

Use Google Search to gather REAL, UP-TO-DATE information from roughly the last 24-48 \
hours. Do NOT invent, guess, or use stale knowledge. Every item must reflect something \
that actually happened recently. Prefer primary/reputable sources. Include real \
numbers (prices, %, dates) where relevant.

Produce ONLY an HTML fragment (no <html>, <head>, or <body> tags, no markdown code \
fences). Use the EXACT section structure below. Each section is an <h2> followed by \
either a <ul> of concise bullets or an HTML <table>. Keep it tight and scannable — \
the whole email should be readable in about 3 minutes. Bold the key entity in each \
bullet using <strong>. Keep each bullet to one or two sentences.

Structure:

<h2>🤖 AI Innovation</h2>
<ul> 3-5 bullets on the biggest AI product / model / company moves in the last 24-48h </ul>

<h2>📄 Research & New Papers</h2>
<ul> 3-4 bullets on notable new papers or research (arXiv, labs). For each, one line on \
what it is and one short "Why it matters" clause. </ul>

<h2>🏛️ Politics</h2>
<ul> 3-4 bullets on key US political / policy developments </ul>

<h2>🌍 Geopolitics</h2>
<ul> 3-4 bullets on global power dynamics, conflicts, trade, or diplomacy </ul>

<h2>📈 Tech Stocks Watch</h2>
Give an HTML table with columns: Ticker, Company, Move, Catalyst. Include 5-6 notable \
tech names with their most recent daily move (e.g. +2.3%) and a short catalyst. Use \
real, recent figures.

<h2>👀 Stocks to Watch</h2>
<ul> 2-3 bullets, each a ticker/company and a concrete reason to keep an eye on it \
this week </ul>

<h2>💡 One Thing to Know</h2>
<p> One short paragraph teaching a useful AI concept, term, or technique to deepen the \
reader's fluency in the field. Make it genuinely educational. </p>

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
