"""Durable local quotas for direct Gemini requests routed through ContextRoute."""

from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, time as wall_time
from email.utils import parsedate_to_datetime
import math
from pathlib import Path
import sqlite3
import time
from zoneinfo import ZoneInfo

MODEL = "gemini-3.5-flash-lite"
MODEL_KEY = "google/" + MODEL
PACIFIC_ZONE = "America/Los_Angeles"


@dataclass(frozen=True)
class Limits:
    rpm: int = 10
    tpm: int = 200_000
    rpd: int = 400

    def __post_init__(self):
        for name, ceiling in (("rpm", 10), ("tpm", 200_000), ("rpd", 400)):
            amount = getattr(self, name)
            if type(amount) is not int or not 1 <= amount <= ceiling:
                raise ValueError(f"Gemini {name} must be an integer from 1 through {ceiling}.")


class QuotaBlocked(Exception):
    def __init__(self, reason, retry_at=None):
        super().__init__(reason)
        self.reason = reason
        self.retry_at = retry_at


def pacific_day(now):
    pacific = ZoneInfo(PACIFIC_ZONE)
    today = datetime.fromtimestamp(now, pacific).date()
    reset = datetime.combine(today + timedelta(days=1), wall_time.min, pacific).timestamp()
    return today.isoformat(), reset


def provider_pause(now, header, details):
    """Use the latest provider delay. Unknown or daily 429s stop until Pacific reset."""
    retry_at = None
    if header:
        try:
            retry_at = now + max(0, int(header))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(header)
                if parsed.tzinfo is not None:
                    retry_at = parsed.timestamp()
            except (ValueError, TypeError, OverflowError):
                pass
    daily = False
    detail_rows = details.get("error", {}).get("details") or [] if isinstance(details, dict) else []
    for row in detail_rows:
        if not isinstance(row, dict):
            continue
        for violation in row.get("violations", []):
            label = str(violation.get("quotaId", "")).lower()
            daily |= "perday" in label or "daily" in label
        duration = row.get("retryDelay")
        if isinstance(duration, str) and duration.endswith("s"):
            try:
                seconds = float(duration[:-1])
                if math.isfinite(seconds) and seconds >= 0:
                    retry_at = max(retry_at or now, now + seconds)
            except ValueError:
                pass
    if daily or retry_at is None:
        retry_at = max(retry_at or now, pacific_day(now)[1])
    return max(now + 1, retry_at)


class Ledger:
    def __init__(self, path: Path, limits: Limits, *, clock=time.time, sleep=time.sleep):
        self.path = path
        self.limits = limits
        self.clock = clock
        self.sleep = sleep
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS requests (
                    id INTEGER PRIMARY KEY, attempt TEXT NOT NULL, at REAL NOT NULL, day TEXT NOT NULL,
                    kind TEXT NOT NULL, reserved INTEGER NOT NULL, estimated INTEGER, status INTEGER,
                    input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER, model_version TEXT
                );
                CREATE INDEX IF NOT EXISTS requests_at ON requests(at);
                CREATE INDEX IF NOT EXISTS requests_day ON requests(day);
                CREATE INDEX IF NOT EXISTS requests_attempt ON requests(attempt);
                CREATE TABLE IF NOT EXISTS pauses (id INTEGER PRIMARY KEY CHECK(id = 1), until REAL NOT NULL);
            """)

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            yield db

    def snapshot(self):
        now = self.clock()
        day, reset = pacific_day(now)
        with self.connect() as db:
            daily, = db.execute("SELECT count(*) FROM requests WHERE day=?", (day,)).fetchone()
            rpm, tpm = db.execute(
                "SELECT count(*), coalesce(sum(reserved),0) FROM requests WHERE at>?", (now-60,)
            ).fetchone()
            pause = db.execute("SELECT until FROM pauses WHERE id=1 AND until>?", (now,)).fetchone()
        return {"pacific_day": day, "daily_requests": daily, "remaining_rpd": max(0, self.limits.rpd-daily),
                "remaining_rpm": max(0, self.limits.rpm-rpm), "remaining_tpm": max(0, self.limits.tpm-tpm),
                "reset_at_utc_epoch": reset, "provider_block_until_utc_epoch": pause[0] if pause else None}

    def ready(self, minimum_requests=2):
        state = self.snapshot()
        if state["provider_block_until_utc_epoch"]:
            raise QuotaBlocked("provider_429", state["provider_block_until_utc_epoch"])
        if state["remaining_rpd"] < minimum_requests:
            raise QuotaBlocked("daily_budget_exhausted", state["reset_at_utc_epoch"])
        return state

    def reserve(self, attempt, kind, tokens=0, estimate=None, cancelled=lambda: False):
        if tokens > self.limits.tpm:
            raise QuotaBlocked("request_exceeds_token_budget")
        while not cancelled():
            now = self.clock()
            day, reset = pacific_day(now)
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                pause = db.execute("SELECT until FROM pauses WHERE id=1 AND until>?", (now,)).fetchone()
                if pause:
                    raise QuotaBlocked("provider_429", pause[0])
                daily, = db.execute("SELECT count(*) FROM requests WHERE day=?", (day,)).fetchone()
                if daily >= self.limits.rpd:
                    raise QuotaBlocked("daily_budget_exhausted", reset)
                window = db.execute("SELECT at,reserved FROM requests WHERE at>? ORDER BY at", (now-60,)).fetchall()
                used = sum(row[1] for row in window)
                if len(window) < self.limits.rpm and used + tokens <= self.limits.tpm:
                    return db.execute("INSERT INTO requests(attempt,at,day,kind,reserved,estimated) VALUES (?,?,?,?,?,?)",
                                      (attempt, now, day, kind, tokens, estimate)).lastrowid
                # Wait outside the transaction so accounting in other runs can progress.
                delay = max(0.01, window[0][0] + 60 - now)
            self.sleep(min(delay, 1))
        raise QuotaBlocked("execution_stopped")

    def pause(self, until):
        with self.connect() as db:
            db.execute("INSERT INTO pauses VALUES(1,?) ON CONFLICT(id) DO UPDATE SET until=max(until,excluded.until)", (until,))

    def settle(self, request_id, status, usage=None, model_version=None):
        inp = out = total = None
        if usage:
            def count(name):
                value = usage.get(name)
                return value if type(value) is int and value >= 0 else None
            inp = count("promptTokenCount")
            out = count("candidatesTokenCount")
            total = count("totalTokenCount")
            if inp is not None:
                inp += count("toolUsePromptTokenCount") or 0
            if out is not None or type(usage.get("thoughtsTokenCount")) is int:
                out = (out or 0) + (count("thoughtsTokenCount") or 0)
        with self.connect() as db:
            db.execute("""UPDATE requests SET status=?, input_tokens=?, output_tokens=?, total_tokens=?, model_version=?,
                          reserved=max(reserved,coalesce(?,0)) WHERE id=?""",
                       (status, inp, out, total, model_version, inp, request_id))

    def attempts(self, attempt):
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT * FROM requests WHERE attempt=? ORDER BY id", (attempt,)).fetchall()
        return [dict(row) for row in rows]

    def metrics(self, attempt):
        rows = self.attempts(attempt)
        generations = [r for r in rows if r["kind"] == "generate"]
        observed = [r for r in generations if r["input_tokens"] is not None and r["total_tokens"] is not None]
        return {"api_requests": len(rows), "generation_requests": len(generations),
                "token_count_requests": len(rows)-len(generations), "generation_requests_with_usage": len(observed),
                "estimated_input_tokens": sum(r["estimated"] or 0 for r in generations),
                "observed_input_tokens": sum(r["input_tokens"] for r in observed) if observed else None,
                "observed_output_tokens": sum(r["output_tokens"] or 0 for r in observed) if observed and all(
                    r["output_tokens"] is not None for r in observed) else None,
                "observed_total_tokens": sum(r["total_tokens"] for r in observed) if observed else None,
                "provider_429_count": sum(r["status"] == 429 for r in rows),
                "upstream_statuses": sorted({r["status"] for r in rows if r["status"] is not None}),
                "response_model_versions": sorted({r["model_version"] for r in observed if r["model_version"]}),
                "quota": self.snapshot()}

