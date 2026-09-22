"""
Job listing scorer -- a small local website built on the TypeSafe (Jev) API.

You paste your target role/skills once, click "Fetch listings" and it pulls
real, recent (last 7 days) job postings from public sources and scores each
one for fit, urgency, and red flags -- shown as cards you can click straight
through to the live posting. It never applies to or submits anything for
you.

Setup:
    pip install flask
    pip install typesafe-sdk
    export TYPESAFE_API_KEY=your_key_here

Run:
    python app.py
    (then open http://localhost:5050 in your browser)
"""

import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, jsonify, render_template, request
from google import genai
from google.genai import types as genai_types
from pypdf import PdfReader
from typesafe_sdk import Choice, Noul, Score, TypeSafeClient

import store
from fetch_jobs import fetch_all

MAX_PROFILE_CHARS = 6000

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[a-zA-Z]{2,}")
PHONE_RE = re.compile(r"(?<!\d)(\+?\d[\d\-\s().]{7,}\d)(?!\d)")


def redact_pii(text):
    """This app's profile/resume text is sent to two external APIs (Jev,
    Gemini) on every run -- that's necessary for scoring, but an email and
    phone number never are. Strip them before anything leaves the machine.
    Professional links (GitHub/LinkedIn) are left alone -- a resume puts
    those there to be shared."""
    text = EMAIL_RE.sub("[email redacted]", text)
    text = PHONE_RE.sub("[phone redacted]", text)
    return text


if not os.environ.get("TYPESAFE_API_KEY"):
    sys.exit("Set TYPESAFE_API_KEY first, e.g.: export TYPESAFE_API_KEY=your_key_here")

app = Flask(__name__)
client = TypeSafeClient()
store.init_db()

# Jev (TypeSafe) can't write text -- it only answers typed questions. Cover
# letter drafting needs an actual generative model, so this one feature uses
# Gemini's free tier instead. GEMINI_API_KEY is optional: everything else in
# this app works without it, only /draft_cover_letter needs it.
_gemini_client = None


def get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            return None
        # Google's free-tier flash models can be genuinely overloaded; the SDK's
        # default retry/backoff can otherwise burn 30-45s on a single call before
        # giving up. Fail fast instead so one slow call doesn't stall everything.
        _gemini_client = genai.Client(
            api_key=api_key,
            http_options=genai_types.HttpOptions(
                timeout=12000,
                retryOptions=genai_types.HttpRetryOptions(attempts=2, initialDelay=1, maxDelay=4),
            ),
        )
    return _gemini_client


def gemini_cap_exceeded():
    return store.usage_today()["gemini"]["calls"] >= DAILY_GEMINI_CALL_CAP


def daily_gemini_cap_message():
    return (
        f"Daily Gemini call budget reached ({DAILY_GEMINI_CALL_CAP} calls used today). "
        "Resets at midnight UTC. Raise DAILY_GEMINI_CALL_CAP in app.py if you want a higher cap."
    )


MATCH_CHECK_LIMIT = 8  # how many top-fit listings get the (slower) Gemini comparison -- kept
# small and equal to the thread pool size so it's one parallel wave, not several sequential
# batches. Gemini's free tier can be genuinely slow/retried under load, unlike Jev.

# Cost/rate guardrails. TypeSafe is billed on input tokens ($0.042/million as of
# writing); Gemini's free tier is rate-limited rather than billed, but both can
# balloon silently if a large multi-source fetch triggers far more calls than
# intended -- an LLM council review of this repo flagged exactly that.
MAX_JEV_CALLS_PER_RUN = 150  # hard cap per single /fetch, regardless of how many
# sources/companies are selected or how high limit_per_source is set
DAILY_JEV_TOKEN_CAP = 3_000_000  # ~$0.13/day at $0.042/million input tokens
DAILY_GEMINI_CALL_CAP = 100  # stay well under typical free-tier daily quotas


def get_resume_matches(gemini, profile, title, description):
    """Returns the list of job requirements the resume actually, specifically
    supports -- grounded comparison, not a generic rewrite. Raises on failure;
    callers decide how to handle that."""
    prompt = f"""Compare this candidate's resume/profile against this specific job posting's requirements.

Candidate resume/profile:
{profile[:4000]}

Job posting:
Title: {title}
Description: {description[:2500]}

List ONLY the requirements, skills, or responsibilities from the job posting that the candidate's resume actually and specifically supports. Do not list anything the resume doesn't clearly back up -- do not infer, assume, or give credit for "related" or "transferable" experience unless the resume states it. If nothing genuinely matches, return an empty list.

Respond with ONLY valid JSON in this exact shape, no markdown code fences, no extra text:
{{"matches": [{{"requirement": "the job requirement, in a few words", "evidence": "short quote or paraphrase from the resume that supports it"}}]}}"""

    response = gemini.models.generate_content(
        model="gemini-flash-lite-latest",
        contents=prompt,
        config={"response_mime_type": "application/json"},
    )
    usage = response.usage_metadata
    store.log_usage("gemini", usage.prompt_token_count if usage else 0)
    raw = (response.text or "").strip()
    return json.loads(raw).get("matches", [])

QUESTIONS = {
    "fit": Score(
        instructions="How well this job matches the candidate's target role, skills, AND experience level",
        criteria=["Poor fit", "Decent fit, worth a look", "Strong fit"],
    ),
    "urgency": Noul(
        instructions="This posting signals urgency to apply soon (recently posted, closing date mentioned, limited openings)",
    ),
    "red_flag": Noul(
        instructions="This listing has red flags of a low-quality or scammy posting (vague responsibilities, no real company info, unrealistic promises, generic copy-paste requirements)",
    ),
    "seniority_mismatch": Noul(
        instructions="This job requires meaningfully more years of experience or a higher seniority level (e.g. senior, staff, lead, principal, director) than the candidate's profile shows they actually have -- a strong tech-stack overlap does not make up for being underqualified on seniority",
    ),
}


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/parse_resume", methods=["POST"])
def parse_resume():
    """Extract text from an uploaded PDF resume so it can be used as the
    matching profile directly -- role, skills, and experience come from
    what's actually on the resume, not a hand-typed summary."""
    file = request.files.get("resume")
    if not file or not file.filename:
        return jsonify({"error": "No file uploaded."}), 400
    if not file.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Only PDF resumes are supported right now."}), 400

    try:
        reader = PdfReader(file.stream)
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as e:
        return jsonify({"error": f"Could not read that PDF: {e}"}), 400

    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return jsonify({"error": "No extractable text found in that PDF (it may be a scanned image)."}), 400

    return jsonify({"profile_text": text[:MAX_PROFILE_CHARS]})


@app.route("/fetch", methods=["POST"])
def fetch():
    """Pull recent listings and score them in one pass -- the site shows
    result cards directly from this call, no intermediate paste step."""
    data = request.get_json(force=True)
    profile = redact_pii((data.get("profile") or "").strip())
    sources = data.get("sources") or []
    greenhouse = [c for c in (data.get("greenhouse") or "").split(",") if c.strip()]
    lever = [c for c in (data.get("lever") or "").split(",") if c.strip()]
    limit_per_source = int(data.get("limit_per_source") or 50)

    if not profile:
        return jsonify({"error": "Enter your target role/skills first."}), 400
    if not sources and not greenhouse and not lever:
        return jsonify({"error": "Pick at least one source or enter a company."}), 400

    listings = fetch_all(sources, greenhouse, lever, limit_per_source)
    if not listings:
        return jsonify({"error": "No listings from the last 7 days came back. Try different sources/companies."}), 400

    usage_before = store.usage_today()
    if usage_before["jev"]["tokens"] >= DAILY_JEV_TOKEN_CAP:
        return jsonify(
            {
                "error": (
                    f"Daily Jev budget reached ({usage_before['jev']['tokens']:,} input tokens used today, "
                    f"cap is {DAILY_JEV_TOKEN_CAP:,}). Resets at midnight UTC. Raise DAILY_JEV_TOKEN_CAP in "
                    "app.py if you want a higher cap."
                )
            }
        ), 429

    # Persistence: a listing already scored against this exact profile doesn't
    # need scoring again, and one already dismissed/applied should never come
    # back. Without this, every run re-pays for and re-shows the same postings.
    phash = store.profile_hash(profile)
    urls = [l["url"] for l in listings if l.get("url")]
    cached_rows = store.get_many(urls)

    def result_from_cache(cached):
        return {
            "title": cached["title"],
            "url": cached["url"],
            "location": cached["location"],
            "description": cached["description"],
            "source": cached["source"],
            "posted_at": cached["posted_at"] or None,
            "fit": cached["fit"],
            "fit_confidence": cached["fit_confidence"],
            "urgent": bool(cached["urgent"]),
            "red_flag": bool(cached["red_flag"]),
            "seniority_mismatch": bool(cached["seniority_mismatch"]),
            "_cached_verified": bool(cached["verified"]),
            "_cached_matches": store.matches_from_json(cached["matches_json"]),
        }

    to_score = []
    reused = []
    for listing in listings:
        cached = cached_rows.get(listing.get("url"))
        if cached and cached["status"] in ("dismissed", "applied"):
            continue  # already acted on -- never resurface
        if cached and cached["profile_hash"] == phash and cached["fit"] is not None:
            reused.append(result_from_cache(cached))
        else:
            to_score.append(listing)

    # Per-run cap: a large multi-source fetch with a high limit_per_source
    # could otherwise trigger hundreds of Jev calls in one request. Score the
    # first MAX_JEV_CALLS_PER_RUN and note the rest were skipped rather than
    # silently scoring an unbounded number.
    skipped_for_cap = 0
    if len(to_score) > MAX_JEV_CALLS_PER_RUN:
        skipped_for_cap = len(to_score) - MAX_JEV_CALLS_PER_RUN
        to_score = to_score[:MAX_JEV_CALLS_PER_RUN]

    questions = dict(QUESTIONS)
    questions["fit"] = Score(
        instructions=(
            "How well this job matches the candidate's target role, skills, AND experience level. "
            "A job that overlaps on tech stack but requires significantly more years of experience or a "
            "higher seniority (senior/staff/lead/principal/director) than the candidate has is a POOR fit, "
            "not a strong one, regardless of keyword overlap. "
            f"Candidate profile: {profile}"
        ),
        criteria=QUESTIONS["fit"].criteria,
    )
    questions["seniority_mismatch"] = Noul(
        instructions=(
            "This job requires meaningfully more years of experience or a higher seniority level "
            "(e.g. senior, staff, lead, principal, director) than the candidate's profile shows they "
            f"actually have. Candidate profile: {profile}"
        ),
    )

    def score_listing(listing):
        state = f"Job title: {listing['title']}\nLocation: {listing.get('location', '')}\n\n{listing['description']}"
        response = client.system_one(state=state, questions=questions)
        store.log_usage("jev", response.usage.input_tokens)
        answers = response.answers
        posted_at = listing.get("posted_at")
        return {
            "title": listing["title"],
            "url": listing.get("url", ""),
            "location": listing.get("location", "Not specified"),
            "description": listing.get("description", ""),
            "source": listing.get("source", ""),
            "posted_at": posted_at.isoformat() if posted_at else None,
            "fit": round(answers["fit"].score, 2),
            "fit_confidence": round(answers["fit"].confidence, 2),
            "urgent": answers["urgency"].noul > 0.5,
            "red_flag": answers["red_flag"].noul > 0.5,
            "seniority_mismatch": answers["seniority_mismatch"].noul > 0.5,
            "_cached_verified": False,
            "_cached_matches": None,
        }

    if to_score:
        with ThreadPoolExecutor(max_workers=10) as pool:
            freshly_scored = list(pool.map(score_listing, to_score))
    else:
        freshly_scored = []

    for r in freshly_scored:
        if not r["url"]:
            continue
        store.upsert(
            r["url"],
            source=r["source"],
            title=r["title"],
            location=r["location"],
            description=r["description"],
            posted_at=r["posted_at"] or "",
            profile_hash=phash,
            fit=r["fit"],
            fit_confidence=r["fit_confidence"],
            urgent=int(r["urgent"]),
            red_flag=int(r["red_flag"]),
            seniority_mismatch=int(r["seniority_mismatch"]),
        )

    results = reused + freshly_scored
    reused_count = len(reused)

    # Seniority mismatches sink to the bottom regardless of tech-stack fit --
    # a "Staff Engineer" posting isn't a good match just because it name-drops
    # your stack.
    results.sort(key=lambda r: (r["seniority_mismatch"], -r["fit"]))

    # Prefer showing a card once the resume is confirmed (via Gemini) to
    # actually match it. When Gemini can't be reached or times out -- it's
    # a free tier, it does happen -- fall back to Jev's own fit score
    # instead of hiding everything, but label those clearly as unverified.
    FIT_FALLBACK_THRESHOLD = 0.5  # some real signal, not "no fit" -- kept low since
    # this is a fallback bar, not the seniority/red-flag gate below

    def passes_fallback(r):
        # Match how every other signal in this app works: flags demote/label,
        # they don't hide. seniority_mismatch/red_flag still show as tags on
        # the card either way -- the person judges, same as verified results.
        return r["fit"] >= FIT_FALLBACK_THRESHOLD

    def check(r):
        try:
            matches = get_resume_matches(gemini, profile, r["title"], r["description"])
            return r, matches, True
        except Exception:
            return r, None, False  # Gemini failed/timed out -- not the same as "no match"

    def apply_verified(r, matches):
        # Persisted either way -- a confirmed "no match" shouldn't cost another
        # Gemini call next run either, same as a confirmed match.
        store.upsert(r["url"], verified=1, matches_json=store.matches_to_json(matches))
        if matches:
            r["matches"] = matches
            r["verified"] = True
            return True
        return False

    def apply_fallback(r):
        if not passes_fallback(r):
            return False
        r["matches"] = []
        r["verified"] = False
        return True

    gemini = get_gemini_client()
    gemini_budget_left = DAILY_GEMINI_CALL_CAP - usage_before["gemini"]["calls"]
    gemini_capped = gemini_budget_left <= 0
    matched_results = []
    unverified_count = 0
    gemini_cache_hits = 0

    for idx, tier in enumerate((results[:MATCH_CHECK_LIMIT], results[MATCH_CHECK_LIMIT:])):
        is_top_tier = idx == 0

        already_verified = [r for r in tier if r["_cached_verified"]]
        needs_check = [r for r in tier if not r["_cached_verified"]]

        for r in already_verified:
            gemini_cache_hits += 1
            matches = r["_cached_matches"] or []
            if matches:
                r["matches"] = matches
                r["verified"] = True
                matched_results.append(r)
            elif apply_fallback(r):
                unverified_count += 1
                matched_results.append(r)

        # Only the top tier pays for a live Gemini attempt -- the (slow,
        # capped) comparison call. The rest fall back to Jev's free fit score.
        if is_top_tier and gemini is not None and needs_check and not gemini_capped:
            with ThreadPoolExecutor(max_workers=8) as pool:
                checked = list(pool.map(check, needs_check))
        else:
            checked = [(r, None, False) for r in needs_check]

        for r, matches, verified in checked:
            if verified:
                if apply_verified(r, matches):
                    matched_results.append(r)
            elif apply_fallback(r):
                unverified_count += 1
                matched_results.append(r)

    matched_results.sort(key=lambda r: (r["seniority_mismatch"], not r["verified"], -r["fit"]))
    for r in matched_results:
        r.pop("_cached_verified", None)
        r.pop("_cached_matches", None)

    return jsonify(
        {
            "unverified_count": unverified_count,
            "reused_count": reused_count,
            "gemini_cache_hits": gemini_cache_hits,
            "results": matched_results,
            "count": len(matched_results),
            "checked": min(len(results), MATCH_CHECK_LIMIT),
            "total_fetched": len(results),
            "skipped_for_cap": skipped_for_cap,
            "gemini_capped": gemini_capped,
            "usage_today": store.usage_today(),
        }
    )


@app.route("/listing/status", methods=["POST"])
def listing_status():
    """Marks a listing dismissed or applied so it stops resurfacing in every
    future fetch, regardless of resume/profile changes."""
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    status = (data.get("status") or "").strip()
    if not url or status not in ("dismissed", "applied", "new"):
        return jsonify({"error": "Invalid url or status."}), 400
    store.set_status(url, status)
    return jsonify({"ok": True})


@app.route("/draft_cover_letter", methods=["POST"])
def draft_cover_letter():
    """Drafts a cover letter for one job. Never sends or submits anything --
    the draft comes back as text for the person to review, edit, and submit
    themselves through the actual posting."""
    gemini = get_gemini_client()
    if gemini is None:
        return jsonify(
            {"error": "Set GEMINI_API_KEY to enable cover letter drafts. Free key: https://aistudio.google.com/apikey"}
        ), 400
    if gemini_cap_exceeded():
        return jsonify({"error": daily_gemini_cap_message()}), 429

    data = request.get_json(force=True)
    profile = redact_pii((data.get("profile") or "").strip())
    title = (data.get("title") or "").strip()
    description = (data.get("description") or "").strip()
    location = (data.get("location") or "").strip()

    if not profile or not title:
        return jsonify({"error": "Missing profile or job details."}), 400

    prompt = f"""Write a concise, professional cover letter for this job application.

Candidate background (from their resume/profile):
{profile[:4000]}

Job:
Title: {title}
Location: {location}
Description: {description[:2000]}

Rules:
- Only use facts about the candidate that appear in their background above. Never invent experience, employers, degrees, or skills that aren't there.
- 3-4 short paragraphs, under 300 words total.
- Plain text, no markdown, no placeholder brackets like [Company Name] -- use the actual job title/details given.
- Professional but not generic-sounding; reference something specific from the job description.
- End with a simple sign-off, no signature block."""

    try:
        response = gemini.models.generate_content(model="gemini-flash-lite-latest", contents=prompt)
        usage = response.usage_metadata
        store.log_usage("gemini", usage.prompt_token_count if usage else 0)
        draft = (response.text or "").strip()
    except Exception as e:
        return jsonify({"error": f"Draft generation failed: {e}"}), 502

    if not draft:
        return jsonify({"error": "Gemini returned an empty draft. Try again."}), 502

    return jsonify({"draft": draft})


@app.route("/match_breakdown", methods=["POST"])
def match_breakdown():
    """Shows only what the resume actually matches against this specific
    job's requirements -- not a rewrite, just a grounded comparison."""
    gemini = get_gemini_client()
    if gemini is None:
        return jsonify(
            {"error": "Set GEMINI_API_KEY to enable this. Free key: https://aistudio.google.com/apikey"}
        ), 400
    if gemini_cap_exceeded():
        return jsonify({"error": daily_gemini_cap_message()}), 429

    data = request.get_json(force=True)
    profile = redact_pii((data.get("profile") or "").strip())
    title = (data.get("title") or "").strip()
    description = (data.get("description") or "").strip()

    if not profile or not title:
        return jsonify({"error": "Missing profile or job details."}), 400

    try:
        matches = get_resume_matches(gemini, profile, title, description)
    except Exception as e:
        return jsonify({"error": f"Comparison failed: {e}"}), 502

    return jsonify({"matches": matches})


if __name__ == "__main__":
    app.run(port=5050, debug=True, threaded=True)
