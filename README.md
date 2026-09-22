# Jev Job Scorer

A local web app that fetches real, recent job listings from public job boards, scores each one
against your resume using [TypeSafe's Jev model](https://typesafe.ai), and shows only the ones
that actually match — with confidence, not vibes.

It never applies to or submits anything on your behalf. Every result links to the real posting so
you apply yourself.

## What it does

- **Upload your resume** (PDF) and it extracts your role, skills, and experience automatically —
  the extracted text is always shown in the profile box for you to review before anything is
  scored, and flagged explicitly if it looks garbled (common with multi-column layouts) or
  suspiciously short (a scanned image with no real text layer)
- **Fetches live listings** from RemoteOK, We Work Remotely, Remotive, Arbeitnow, Jobicy, and any
  Greenhouse, Lever, Ashby, or Workable company job board you name — only listings from the last 7
  days
- **Scores every listing with Jev** for fit, seniority match, urgency, and scam/red-flag signals
- **Compares your resume against each top match** (via Gemini, since this step needs an actual
  generative model — Jev only answers typed questions, it can't write free text) and shows only
  the specific requirements your resume actually backs up
- **Drafts a cover letter** per listing, grounded only in what's actually on your resume
- Falls back gracefully to Jev's own fit score (clearly labeled "not yet verified") if the resume
  comparison step is unavailable, instead of hiding everything — and if that fallback has been the
  case for more than 24 hours (a lapsed key, an exhausted quota), a persistent banner says so
  instead of it silently becoming the permanent state across sessions
- **Remembers what you've already seen.** Scores are cached locally (SQLite, `data.db`) per
  resume, so re-running doesn't re-pay for or re-show the same listings. Mark a listing "applied"
  or "not interested" and it's gone for good, regardless of future fetches

## Why Jev, not just an LLM

[Jev](https://typesafe.ai) is a "System One" model: it only answers typed questions (yes/no, a
choice from options, or a rating on a scale) with calibrated probabilities — no free text, no
hallucinated prose. That makes it fast and cheap for judgment calls like "does this job need more
seniority than this candidate has," but it genuinely can't write a cover letter or compare
unstructured resume text against unstructured job text — those need a real generative model, which
is why this project pairs Jev (structured judgment) with Gemini (the one generative step) rather
than using either alone.

## Setup

```bash
pip install flask typesafe-sdk pypdf google-genai requests

export TYPESAFE_API_KEY=your_key_here   # https://console.typesafe.ai/settings/keys
export GEMINI_API_KEY=your_key_here     # free tier: https://aistudio.google.com/apikey (optional)

python app.py
# open http://localhost:5050
```

`GEMINI_API_KEY` is optional — everything works without it except the resume-comparison and
cover-letter features, which fail with a clear message telling you how to enable them.

## Privacy

This is a local app (binds to `127.0.0.1`, no auth, no account) but it is **not** offline: your
resume/profile text is sent to two external APIs on every run —

- **TypeSafe (Jev)** — every listing's score (fit, seniority, red-flag, urgency) includes your
  profile in the prompt
- **Google (Gemini)** — the resume-comparison and cover-letter features send your profile text too

Email addresses and phone numbers are automatically redacted from the profile before either call
is made (job-fit scoring never needs them). Nothing else is redacted — the rest of your resume
text, including work history and any other identifying details it contains, does leave the
machine. If that's not acceptable for your resume, edit the profile text box down to just the
role/skills summary before fetching, instead of using the full extracted PDF text.

`data.db` (the local cache) and any uploaded resume text stay on your machine — those are never
sent anywhere beyond the two API calls above, and `data.db` is gitignored.

## Cost/rate limits

TypeSafe bills on input tokens (~$0.042/million as of writing); Gemini's free tier is rate-limited
rather than billed. Both are guarded so a large multi-source fetch can't silently balloon into
hundreds of calls:

- **Per-run cap** — at most `MAX_JEV_CALLS_PER_RUN` (150) Jev calls in a single fetch, regardless
  of how many sources/companies are selected
- **Daily caps** — `DAILY_JEV_TOKEN_CAP` (3M tokens, ~$0.13) and `DAILY_GEMINI_CALL_CAP` (100
  calls), tracked in `data.db`, reset at midnight UTC. Hitting either returns a clear error instead
  of failing silently
- Actual usage (calls + tokens, today) is shown after every fetch — all constants are in `app.py`
  if you want to raise or lower them

## Sources

Only public, ToS-friendly job feeds are used — LinkedIn and Indeed aren't included since neither
offers a public API and scraping them violates their terms of service.

| Source | Notes |
|---|---|
| RemoteOK | Public JSON API |
| We Work Remotely | Public RSS |
| Remotive | Free tier is personal-use only, rate-limited |
| Arbeitnow | Public API |
| Jobicy | Public API |
| HN "Who is hiring" | Public Algolia API, optional |
| Greenhouse / Lever / Ashby / Workable | Any company slug you provide (find it in that company's own careers-page URL) |

Adding another site: any company running its careers page on one of those four ATS platforms
works out of the box — just type its slug into the matching field, no code change needed. A
company on a different ATS (or a custom-built careers page) would need a new fetcher function in
`fetch_jobs.py`, following the same pattern as `fetch_ashby`/`fetch_workable`, provided that
platform has a public, unauthenticated JSON endpoint (scraping a page that isn't meant to be
machine-read is out of scope, same reasoning as the LinkedIn/Indeed exclusion above).
