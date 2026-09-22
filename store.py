"""
Local persistence for the job scorer -- SQLite, stdlib only, one file
(data.db, gitignored). Fixes the "resets every run" problem: without this,
every fetch re-scores every listing with Jev and re-checks every top
candidate with Gemini, even ones seen (and paid for) in a prior run, and a
job you already dismissed or applied to keeps resurfacing forever.

Keyed by the listing's URL -- stable and unique per posting across every
source this app fetches from, so no per-source ID scheme is needed.
"""

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "data.db")


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(_connect()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS listings (
                url TEXT PRIMARY KEY,
                source TEXT,
                title TEXT,
                location TEXT,
                description TEXT,
                posted_at TEXT,
                profile_hash TEXT,
                fit REAL,
                fit_confidence REAL,
                urgent INTEGER,
                red_flag INTEGER,
                seniority_mismatch INTEGER,
                verified INTEGER,
                matches_json TEXT,
                status TEXT NOT NULL DEFAULT 'new',
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                provider TEXT NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.commit()


def log_usage(provider, input_tokens=0):
    """provider is 'jev' or 'gemini'. Every paid/rate-limited API call gets
    logged so usage_today() can enforce a daily budget instead of this app
    being able to burn an unbounded amount silently."""
    with closing(_connect()) as conn:
        conn.execute(
            "INSERT INTO usage_log (ts, provider, input_tokens) VALUES (?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), provider, input_tokens),
        )
        conn.commit()


def usage_today():
    """Calls and input tokens per provider since midnight UTC."""
    today_start = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00")
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT provider, COUNT(*) AS calls, COALESCE(SUM(input_tokens), 0) AS tokens "
            "FROM usage_log WHERE ts >= ? GROUP BY provider",
            (today_start,),
        ).fetchall()
    usage = {"jev": {"calls": 0, "tokens": 0}, "gemini": {"calls": 0, "tokens": 0}}
    for row in rows:
        usage[row["provider"]] = {"calls": row["calls"], "tokens": row["tokens"]}
    return usage


def profile_hash(profile):
    """Cached scores are only reused while the profile they were scored
    against hasn't changed -- a new resume invalidates old fit scores, but
    never invalidates a dismissed/applied status (that's profile-independent)."""
    return hashlib.sha256(profile.encode("utf-8")).hexdigest()[:16]


def get(url):
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM listings WHERE url = ?", (url,)).fetchone()
        return dict(row) if row else None


def get_many(urls):
    if not urls:
        return {}
    with closing(_connect()) as conn:
        placeholders = ",".join("?" for _ in urls)
        rows = conn.execute(f"SELECT * FROM listings WHERE url IN ({placeholders})", urls).fetchall()
        return {row["url"]: dict(row) for row in rows}


def upsert(url, **fields):
    now = datetime.now(timezone.utc).isoformat()
    existing = get(url)
    with closing(_connect()) as conn:
        if existing:
            fields["last_seen_at"] = now
            set_clause = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(f"UPDATE listings SET {set_clause} WHERE url = ?", (*fields.values(), url))
        else:
            fields.setdefault("status", "new")
            fields["first_seen_at"] = now
            fields["last_seen_at"] = now
            fields["url"] = url
            cols = ", ".join(fields)
            placeholders = ", ".join("?" for _ in fields)
            conn.execute(f"INSERT INTO listings ({cols}) VALUES ({placeholders})", tuple(fields.values()))
        conn.commit()


def set_status(url, status):
    """status is one of 'new', 'dismissed', 'applied'."""
    with closing(_connect()) as conn:
        conn.execute(
            "UPDATE listings SET status = ?, last_seen_at = ? WHERE url = ?",
            (status, datetime.now(timezone.utc).isoformat(), url),
        )
        conn.commit()


def matches_to_json(matches):
    return json.dumps(matches) if matches is not None else None


def matches_from_json(raw):
    return json.loads(raw) if raw else None
