-- Seed the author's account with a compiled_profile hand-derived from the
-- original hardcoded PROMPT, so the existing 8am brief does not regress when
-- the worker switches from a constant to a database row.
--
-- Usage: after the user has signed in once (so a row exists in auth.users),
-- replace :author_id below with that user's uuid, then run this in the
-- Supabase SQL editor. Or set the email and let the ON CONFLICT upsert run.

insert into profiles (id, email, interests_text, compiled_profile, compiled_at,
                      delivery_hour, timezone, wants_push, wants_email, active)
values (
  '7d8b3d94-f2c0-493e-b82b-d37a7001f287',  -- <-- replace with auth.users uuid
  'abhinavballa28@gmail.com',
  'AI innovation, new research and papers, US politics, geopolitics, tech stocks, '
    'and turning all of it into money, businesses, AI automations to build, and '
    'well-timed market bets.',
  '{
    "persona": "an ambitious AI engineer who wants to stay deeply knowledgeable in AI and geopolitics, and to convert that knowledge into money, businesses, AI automations to build, and well-timed market bets",
    "edge_for": "a specific AI automation or product they could build, a new money-making or business opportunity, or a directional market read (a named stock/asset and a potential high or low to position for, framed as a speculative hypothesis, never as guaranteed advice)",
    "sections": [
      {"emoji": "🤖", "title": "AI Innovation", "guidance": "A <ul> of 3-5 bullets on the biggest AI product / model / company moves in the last 24-48h, each with its Edge clause."},
      {"emoji": "📄", "title": "Research & New Papers", "guidance": "A <ul> of 3-4 bullets on notable new papers or research (arXiv, labs). Fact + Edge, where the Edge names what could be built or automated on top of this research."},
      {"emoji": "🏛️", "title": "Politics", "guidance": "A <ul> of 3-4 bullets on key US political / policy developments, each with its Edge clause (policy shifts to who wins/loses, what to build, what to trade)."},
      {"emoji": "🌍", "title": "Geopolitics", "guidance": "A <ul> of 3-4 bullets on global power dynamics, conflicts, trade, or diplomacy, each with its Edge clause."},
      {"emoji": "📈", "title": "Tech Stocks Watch", "guidance": "An HTML <table> with columns Ticker, Company, Move, Catalyst, Angle. 5-6 notable tech names with their most recent daily move (e.g. +2.3%), a short catalyst, and an Angle cell with a concrete speculative read. Use real, recent figures."},
      {"emoji": "👀", "title": "Stocks to Watch", "guidance": "A <ul> of 2-3 bullets, each a ticker/company with a directional thesis: the catalyst, a potential high or low to position for this week, and why now. Mark as speculative."},
      {"emoji": "🎯", "title": "Your Edge — Highest-Conviction Moves", "guidance": "A <ul> of 2-3 bullets synthesizing the single best opportunities from everything above. Each bullet must end with a concrete next action the reader could take this week. Frame all market calls as speculative hypotheses."},
      {"emoji": "💡", "title": "One Thing to Know", "guidance": "A single short <p> teaching a useful AI concept, term, or technique to deepen the reader''s fluency. Make it genuinely educational."},
      {"emoji": "🧠", "title": "Prompt Engineering Move", "guidance": "A single <p> teaching ONE concrete prompt-engineering technique the reader can apply today. Name the technique in <strong>bold</strong>, explain in one or two sentences when and why it works, and include a short before/after or example snippet. Rotate the technique day to day."}
    ]
  }'::jsonb,
  now(),
  8,
  'America/Los_Angeles',
  false,
  true,
  true
)
on conflict (id) do update set
  email            = excluded.email,          -- delivery address for wants_email
  compiled_profile = excluded.compiled_profile,
  compiled_at      = excluded.compiled_at,
  wants_email      = excluded.wants_email,
  active           = excluded.active;
