# Jev Job Scorer

A local web app that fetches real, recent job listings from public job boards, scores each one
against your resume using [TypeSafe's Jev model](https://typesafe.ai), and shows only the ones
that actually match — with confidence, not vibes.

It never applies to or submits anything on your behalf. Every result links to the real posting so
you apply yourself.

## What it does

- **Upload your resume** (PDF) and it extracts your role, skills, and experience automatically
- **Fetches live listings** from RemoteOK, We Work Remotely, Remotive, Arbeitnow, Jobicy, and any
  Greenhouse/Lever company job board you name — only listings from the last 7 days
- **Scores every listing with Jev** for fit, seniority match, urgency, and scam/red-flag signals
- **Compares your resume against each top match** (via Gemini, since this step needs an actual
  generative model — Jev only answers typed questions, it can't write free text) and shows only
  the specific requirements your resume actually backs up
- **Drafts a cover letter** per listing, grounded only in what's actually on your resume
- Falls back gracefully to Jev's own fit score (clearly labeled "not yet verified") if the resume
  comparison step is unavailable, instead of hiding everything

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
| Greenhouse / Lever | Any company slug you provide |
