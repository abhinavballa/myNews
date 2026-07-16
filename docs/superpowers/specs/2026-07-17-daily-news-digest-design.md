# Daily Tech & Geopolitics Digest — Design

**Date:** 2026-07-17
**Repo:** abhinavballa/myNews

## Purpose

Email a concise, up-to-date daily digest to the user at 8:00 AM Pacific so they
stay knowledgeable in AI (innovation, research, papers) and in geopolitics,
politics, and tech stocks. It should be scannable in ~3 minutes and something
worth looking forward to each morning.

## Delivery decisions

- **Scheduling/hosting:** GitHub Actions scheduled workflow (cloud, free, runs
  even when the user's Mac is off).
- **Email delivery:** Gmail SMTP using a Gmail App Password.
- **Sender and recipient:** both `abhinavballa28@gmail.com`.
- **Content model:** `gemini-2.5-flash` with Google Search grounding for live,
  current data every morning.

## Data flow

1. GitHub Actions cron triggers the job.
2. `main.py` gates on real Pacific time (only proceeds at 8am `America/Los_Angeles`).
3. `generate.py` calls Gemini API with the Google Search tool enabled and a
   structured prompt, returning an HTML digest fragment.
4. `email_send.py` wraps the fragment in a styled HTML email template and sends
   it via Gmail SMTP (TLS).

## Email sections (table-per-section, bulleted)

- 🤖 AI Innovation — biggest product/model/company moves in last 24h
- 📄 Research & New Papers — notable arXiv/lab publications + "why it matters"
- 🏛️ Politics — key US + policy developments
- 🌍 Geopolitics — global power dynamics, conflicts, trade
- 📈 Tech Stocks Watch — table: ticker, price move %, catalyst
- 👀 Stocks to Watch — 2-3 names with a reason
- 💡 One Thing to Know — a deeper AI concept/term to level up fluency

Content kept concise and scannable.

## DST handling

GitHub cron is UTC-only and ignores daylight saving. The workflow runs at BOTH
`15:00` and `16:00` UTC. `main.py` computes the current `America/Los_Angeles`
hour and only sends when it equals 8. This yields a true 8am Pacific delivery
in both PST (winter) and PDT (summer). GitHub may delay cron by 5-15 min under
load — acceptable for a morning newsletter.

## Components

```
news_bot/
  main.py          # time gate -> generate -> send; error handling
  generate.py      # Gemini call with Google Search grounding
  email_send.py    # HTML template + SMTP send
requirements.txt
.github/workflows/daily-news.yml
```

## Secrets (GitHub encrypted secrets)

- `GEMINI_API_KEY`
- `GMAIL_ADDRESS` — sender (abhinavballa28@gmail.com)
- `GMAIL_APP_PASSWORD` — 16-char Gmail app password
- `RECIPIENT` — recipient (abhinavballa28@gmail.com)

## Error handling

- If Gemini returns empty/failed content, the job fails loudly (non-zero exit)
  so GitHub surfaces it — no silent empty emails.
- SMTP errors are raised and logged.
- A `--force`/manual dispatch path bypasses the time gate for testing.

## Testing

- `workflow_dispatch` trigger for manual runs.
- Local run via env vars for a smoke test before relying on the schedule.
