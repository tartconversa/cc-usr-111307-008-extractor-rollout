"""Persistent rollout storage: immutable rules, captures, gold truth, reviews.

SQLite is used deliberately: the standard library ships it, transactions give
us compare-and-swap activation and fencing, and every decision can be traced
back to the exact captured bytes and rule digest.

Nothing in this module derives gold truth from an extraction result.  Gold
pages must be supplied with an explicit reviewer and verification time, and
they are keyed by the id of the captured response bytes.  A live capture can
never receive gold: live pages are used for drift hints only.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Sequence
import uuid

from .rules import (
    ReleaseCandidate,
    ReleaseConfig,
    RulePackage,
    RuleValidationError,
    canonical_json,
    is_digest,
    sha256_bytes,
    sha256_json,
)

PRODUCTION = "production"
SHADOW = "shadow"
REVIEW = "review"
SUPERSEDED = "superseded"
QUARANTINED = "quarantined"

SOURCE_LIVE = "live"
SOURCE_ARCHIVE = "archive"
SOURCE_MANUAL = "manual"
SOURCE_TAGS = {SOURCE_LIVE, SOURCE_ARCHIVE, SOURCE_MANUAL}
GOLD_PROVENANCE = {"manual-review", "independent-transcription"}

JOB_PENDING = "pending"
JOB_LEASED = "leased"
JOB_DONE = "done"
JOB_SUPERSEDED = "superseded"

CIRCUIT_CLOSED = "closed"
CIRCUIT_OPEN = "open"

WINDOW_OPEN = "open"
WINDOW_EVALUATED = "evaluated"
WINDOW_ACTIVATED = "activated"
WINDOW_STALE = "stale"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class StoreError(RuntimeError):
    """Base class for rollout storage errors."""


class ImmutableRuleError(StoreError):
    """A version label was already bound to different content."""


class UnknownRuleError(StoreError):
    """A rule digest or version is not registered."""


class GoldError(StoreError):
    """Gold truth is missing, live, or would be overwritten."""


class WindowError(StoreError):
    """A review window is malformed or in the wrong state."""


class _JobRejection(Exception):
    """Internal signal: a job completion failed and must be logged."""

    def __init__(self, reason: str, job_row: sqlite3.Row | None, detail: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.job_row = job_row
        self.detail = detail or {}


class StaleWorkerError(StoreError):
    """A worker result belongs to a superseded rule, version or window."""

    def __init__(self, reason: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


@dataclass(frozen=True)
class Capture:
    capture_id: str
    url: str
    page_family: str
    response_digest: str
    body: bytes
    headers: dict[str, str]
    source_tag: str
    sample_ordinal: int | None
    captured_at: float

    def to_json(self) -> dict[str, Any]:
        return {
            "capture_id": self.capture_id,
            "url": self.url,
            "page_family": self.page_family,
            "response_digest": self.response_digest,
            "source_tag": self.source_tag,
            "sample_ordinal": self.sample_ordinal,
            "captured_at": self.captured_at,
            "bytes": len(self.body),
        }


@dataclass(frozen=True)
class ActiveRule:
    page_family: str
    rule_digest: str
    epoch: int
    activated_at: float
    circuit_state: str
    consecutive_failures: int
    circuit_reason: str | None

    @property
    def circuit_open(self) -> bool:
        return self.circuit_state == CIRCUIT_OPEN

    def to_json(self) -> dict[str, Any]:
        return {
            "page_family": self.page_family,
            "rule_digest": self.rule_digest,
            "epoch": self.epoch,
            "activated_at": self.activated_at,
            "circuit_state": self.circuit_state,
            "consecutive_failures": self.consecutive_failures,
            "circuit_reason": self.circuit_reason,
        }


@dataclass(frozen=True)
class ShadowJob:
    job_id: str
    window_id: str
    capture_id: str
    page_family: str
    rule_digest: str
    baseline_rule_digest: str | None
    extractor_version: str
    status: str
    fence: int
    lease_token: str | None
    worker_id: str | None
    capture: Capture | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "window_id": self.window_id,
            "capture_id": self.capture_id,
            "page_family": self.page_family,
            "rule_digest": self.rule_digest,
            "baseline_rule_digest": self.baseline_rule_digest,
            "extractor_version": self.extractor_version,
            "status": self.status,
            "fence": self.fence,
            "worker_id": self.worker_id,
            "capture": None if self.capture is None else self.capture.to_json(),
        }


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS rule_packages (
    digest TEXT PRIMARY KEY,
    page_family TEXT NOT NULL,
    version TEXT NOT NULL,
    canonical TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE (page_family, version)
);

CREATE TABLE IF NOT EXISTS candidates (
    digest TEXT PRIMARY KEY,
    label TEXT NOT NULL UNIQUE,
    canonical TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS configs (
    digest TEXT PRIMARY KEY,
    canonical TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS captures (
    capture_id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    page_family TEXT NOT NULL,
    response_digest TEXT NOT NULL,
    body BLOB NOT NULL,
    headers TEXT NOT NULL,
    source_tag TEXT NOT NULL CHECK (source_tag IN ('live', 'archive', 'manual')),
    sample_ordinal INTEGER,
    sample_key TEXT NOT NULL,
    captured_at REAL NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS captures_url_digest
    ON captures (url, response_digest, source_tag, sample_key);
CREATE INDEX IF NOT EXISTS captures_family
    ON captures (page_family, source_tag, sample_ordinal);

CREATE TABLE IF NOT EXISTS gold (
    capture_id TEXT PRIMARY KEY REFERENCES captures (capture_id),
    fields TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    verified_at TEXT NOT NULL,
    provenance TEXT NOT NULL CHECK (provenance IN ('manual-review', 'independent-transcription')),
    note TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS active_rules (
    page_family TEXT PRIMARY KEY,
    rule_digest TEXT NOT NULL REFERENCES rule_packages (digest),
    epoch INTEGER NOT NULL CHECK (epoch >= 1),
    activated_at REAL NOT NULL,
    circuit_state TEXT NOT NULL DEFAULT 'closed' CHECK (circuit_state IN ('closed', 'open')),
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    circuit_opened_at REAL,
    circuit_reason TEXT,
    last_failure_capture TEXT
);

CREATE TABLE IF NOT EXISTS activation_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    page_family TEXT NOT NULL,
    from_digest TEXT,
    to_digest TEXT NOT NULL,
    from_epoch INTEGER,
    to_epoch INTEGER NOT NULL,
    window_id TEXT,
    actor TEXT NOT NULL,
    note TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS circuit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    page_family TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('open', 'reset')),
    rule_digest TEXT NOT NULL,
    consecutive_failures INTEGER NOT NULL,
    reason TEXT NOT NULL,
    actor TEXT,
    window_id TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS windows (
    window_id TEXT PRIMARY KEY,
    candidate_digest TEXT NOT NULL REFERENCES candidates (digest),
    config_digest TEXT NOT NULL REFERENCES configs (digest),
    expected_active TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    live_ordinal_start INTEGER NOT NULL,
    live_ordinal_end INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open', 'evaluated', 'activated', 'stale')),
    decision TEXT,
    created_by TEXT NOT NULL,
    note TEXT NOT NULL,
    created_at REAL NOT NULL,
    evaluated_at REAL,
    activated_at REAL,
    invalidated_at REAL,
    stale_reason TEXT
);

CREATE TABLE IF NOT EXISTS window_pages (
    window_id TEXT NOT NULL REFERENCES windows (window_id),
    capture_id TEXT NOT NULL REFERENCES captures (capture_id),
    page_family TEXT NOT NULL,
    split TEXT NOT NULL CHECK (split IN ('gold', 'live')),
    sample_ordinal INTEGER,
    response_digest TEXT NOT NULL,
    gold_snapshot TEXT,
    PRIMARY KEY (window_id, capture_id)
);

CREATE TABLE IF NOT EXISTS shadow_jobs (
    job_id TEXT PRIMARY KEY,
    window_id TEXT NOT NULL REFERENCES windows (window_id),
    capture_id TEXT NOT NULL REFERENCES captures (capture_id),
    page_family TEXT NOT NULL,
    rule_digest TEXT NOT NULL,
    baseline_rule_digest TEXT,
    extractor_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'leased', 'done', 'superseded')),
    fence INTEGER NOT NULL DEFAULT 0,
    lease_token TEXT,
    worker_id TEXT,
    leased_at REAL,
    lease_expires REAL,
    completed_at REAL,
    attempts INTEGER NOT NULL DEFAULT 0,
    UNIQUE (window_id, capture_id)
);

CREATE TABLE IF NOT EXISTS shadow_results (
    job_id TEXT PRIMARY KEY REFERENCES shadow_jobs (job_id),
    window_id TEXT NOT NULL,
    capture_id TEXT NOT NULL,
    page_family TEXT NOT NULL,
    rule_digest TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    response_digest TEXT NOT NULL,
    result TEXT NOT NULL,
    submitted_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS rejected_results (
    rejection_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT,
    window_id TEXT,
    worker_id TEXT,
    rule_digest TEXT,
    extractor_version TEXT,
    reason TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS page_outcomes (
    outcome_id INTEGER PRIMARY KEY AUTOINCREMENT,
    capture_id TEXT NOT NULL REFERENCES captures (capture_id),
    page_family TEXT NOT NULL,
    rule_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    produced INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS page_outcomes_family ON page_outcomes (page_family, created_at);

CREATE TABLE IF NOT EXISTS production_results (
    result_id INTEGER PRIMARY KEY AUTOINCREMENT,
    capture_id TEXT NOT NULL REFERENCES captures (capture_id),
    page_family TEXT NOT NULL,
    rule_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    result TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS production_results_family
    ON production_results (page_family, created_at);

CREATE TABLE IF NOT EXISTS family_policy (
    page_family TEXT PRIMARY KEY,
    circuit_failure_threshold INTEGER NOT NULL CHECK (circuit_failure_threshold >= 1)
);

CREATE TABLE IF NOT EXISTS evaluations (
    window_id TEXT PRIMARY KEY REFERENCES windows (window_id),
    decision TEXT NOT NULL,
    canonical TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""


class Store:
    """A transactionally consistent rollout store.

    The connection is shared but every write runs under a process lock and a
    ``BEGIN IMMEDIATE`` transaction, so compare-and-swap activation is safe
    within and across processes (SQLite locking).
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.path,
            check_same_thread=False,
            isolation_level=None,
            timeout=30,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    @contextmanager
    def _transaction(self):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    @contextmanager
    def _read(self):
        with self._lock:
            yield self._conn

    # ------------------------------------------------------------------
    # Immutable rule packages, candidates and configs
    # ------------------------------------------------------------------
    def register_rule(self, package: RulePackage) -> str:
        if not isinstance(package, RulePackage):
            raise RuleValidationError("register_rule expects a RulePackage")
        payload = package.to_json()
        canonical = canonical_json(payload)
        digest = package.digest
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT digest, canonical FROM rule_packages WHERE page_family = ? AND version = ?",
                (package.page_family, package.version),
            ).fetchone()
            if row is not None:
                if row["digest"] != digest or row["canonical"] != canonical:
                    raise ImmutableRuleError(
                        "rule %s/%s is already bound to a different digest" % (package.page_family, package.version)
                    )
                return digest
            existing = conn.execute("SELECT canonical FROM rule_packages WHERE digest = ?", (digest,)).fetchone()
            if existing is not None:
                return digest
            conn.execute(
                "INSERT INTO rule_packages (digest, page_family, version, canonical, created_at) VALUES (?, ?, ?, ?, ?)",
                (digest, package.page_family, package.version, canonical, time.time()),
            )
        return digest

    def get_rule(self, digest: str) -> RulePackage:
        with self._read() as conn:
            row = conn.execute("SELECT canonical, digest FROM rule_packages WHERE digest = ?", (digest,)).fetchone()
        if row is None or not is_digest(digest):
            raise UnknownRuleError("rule digest %r is not registered" % (digest,))
        payload = json.loads(row["canonical"])
        if sha256_json(payload) != row["digest"]:
            raise StoreError("stored rule payload does not match its digest")
        return RulePackage.from_json(payload)

    def list_rules(self, page_family: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT digest, page_family, version, created_at FROM rule_packages"
        params: tuple[Any, ...] = ()
        if page_family is not None:
            query += " WHERE page_family = ?"
            params = (page_family,)
        query += " ORDER BY page_family, created_at, version"
        with self._read() as conn:
            return [dict(row) for row in conn.execute(query, params)]

    def register_candidate(self, candidate: ReleaseCandidate) -> str:
        if not isinstance(candidate, ReleaseCandidate):
            raise RuleValidationError("register_candidate expects a ReleaseCandidate")
        for package in candidate.rules:
            self.register_rule(package)
        canonical = candidate.to_json()
        digest = candidate.digest
        with self._transaction() as conn:
            row = conn.execute("SELECT digest, canonical FROM candidates WHERE label = ?", (candidate.label,)).fetchone()
            if row is not None:
                if row["digest"] != digest or row["canonical"] != canonical_json(canonical):
                    raise ImmutableRuleError("candidate label %r is already bound to different rules" % candidate.label)
                return digest
            conn.execute(
                "INSERT INTO candidates (digest, label, canonical, created_at) VALUES (?, ?, ?, ?)",
                (digest, candidate.label, canonical_json(canonical), time.time()),
            )
        return digest

    def get_candidate(self, digest: str) -> ReleaseCandidate:
        with self._read() as conn:
            row = conn.execute("SELECT canonical FROM candidates WHERE digest = ?", (digest,)).fetchone()
        if row is None:
            raise StoreError("candidate digest %r is not registered" % (digest,))
        payload = json.loads(row["canonical"])
        if sha256_json(payload) != digest:
            raise StoreError("stored candidate payload does not match its digest")
        families = [item["page_family"] for item in payload.get("rules", [])]
        rules = [self.get_rule(item["rule_digest"]) for item in payload.get("rules", [])]
        if sorted(families) != sorted(rule.page_family for rule in rules):
            raise StoreError("candidate rules do not match their family references")
        return ReleaseCandidate(label=payload["label"], rules=tuple(rules))

    def register_config(self, config: ReleaseConfig) -> str:
        payload = config.to_json()
        canonical = canonical_json(payload)
        digest = config.digest
        with self._transaction() as conn:
            row = conn.execute("SELECT canonical FROM configs WHERE digest = ?", (digest,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO configs (digest, canonical, created_at) VALUES (?, ?, ?)",
                    (digest, canonical, time.time()),
                )
        return digest

    def get_config(self, digest: str) -> ReleaseConfig:
        with self._read() as conn:
            row = conn.execute("SELECT canonical FROM configs WHERE digest = ?", (digest,)).fetchone()
        if row is None:
            raise StoreError("config digest %r is not registered" % (digest,))
        payload = json.loads(row["canonical"])
        if sha256_json(payload) != digest:
            raise StoreError("stored config payload does not match its digest")
        return ReleaseConfig.from_json(payload)

    # ------------------------------------------------------------------
    # Captures and independent gold truth
    # ------------------------------------------------------------------
    def capture(
        self,
        url: str,
        page_family: str,
        body: bytes,
        headers: dict[str, str] | None = None,
        source_tag: str = SOURCE_LIVE,
        sample_ordinal: int | None = None,
    ) -> Capture:
        if not url or not page_family:
            raise StoreError("capture needs a url and page family")
        if source_tag not in SOURCE_TAGS:
            raise StoreError("unknown capture source tag %r" % (source_tag,))
        if not isinstance(body, (bytes, bytearray)):
            raise StoreError("capture body must be bytes; decode only for display")
        body = bytes(body)
        response_digest = sha256_bytes(body)
        if source_tag == SOURCE_LIVE and sample_ordinal is not None and sample_ordinal < 0:
            raise StoreError("sample ordinal must not be negative")
        sample_key = "" if sample_ordinal is None else str(sample_ordinal)
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM captures WHERE url = ? AND response_digest = ? AND source_tag = ? AND sample_key = ?",
                (url, response_digest, source_tag, sample_key),
            ).fetchone()
            if row is not None:
                if row["page_family"] != page_family:
                    raise StoreError(
                        "capture %s is already registered under family %s"
                        % (row["capture_id"], row["page_family"])
                    )
                return self._capture_from_row(row)
            if source_tag == SOURCE_LIVE and sample_ordinal is not None:
                clash = conn.execute(
                    "SELECT capture_id, url FROM captures WHERE page_family = ? AND source_tag = 'live' "
                    "AND sample_ordinal = ?",
                    (page_family, sample_ordinal),
                ).fetchone()
                if clash is not None:
                    # A sample ordinal identifies one live sample.  Two
                    # different payloads under the same ordinal would let a
                    # window silently count one sample twice.
                    raise StoreError(
                        "live sample ordinal %d for family %s is already bound to %s"
                        % (sample_ordinal, page_family, clash["url"])
                    )
            capture_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO captures (capture_id, url, page_family, response_digest, body, headers, "
                "source_tag, sample_ordinal, sample_key, captured_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    capture_id,
                    url,
                    page_family,
                    response_digest,
                    body,
                    canonical_json(headers or {}),
                    source_tag,
                    sample_ordinal,
                    sample_key,
                    time.time(),
                ),
            )
            row = conn.execute("SELECT * FROM captures WHERE capture_id = ?", (capture_id,)).fetchone()
        return self._capture_from_row(row)

    @staticmethod
    def _capture_from_row(row: sqlite3.Row) -> Capture:
        return Capture(
            capture_id=row["capture_id"],
            url=row["url"],
            page_family=row["page_family"],
            response_digest=row["response_digest"],
            body=bytes(row["body"]),
            headers=json.loads(row["headers"]),
            source_tag=row["source_tag"],
            sample_ordinal=row["sample_ordinal"],
            captured_at=row["captured_at"],
        )

    def get_capture(self, capture_id: str) -> Capture:
        with self._read() as conn:
            row = conn.execute("SELECT * FROM captures WHERE capture_id = ?", (capture_id,)).fetchone()
        if row is None:
            raise StoreError("capture %r is not registered" % (capture_id,))
        capture = self._capture_from_row(row)
        if sha256_bytes(capture.body) != capture.response_digest:
            raise StoreError("capture body no longer matches its response digest")
        return capture

    def list_captures(self, page_family: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT capture_id, url, page_family, response_digest, source_tag, sample_ordinal, captured_at FROM captures"
        params: tuple[Any, ...] = ()
        if page_family is not None:
            query += " WHERE page_family = ?"
            params = (page_family,)
        query += " ORDER BY captured_at, capture_id"
        with self._read() as conn:
            return [dict(row) for row in conn.execute(query, params)]

    def set_gold(
        self,
        capture_id: str,
        fields: dict[str, str],
        reviewer: str,
        verified_at: str,
        provenance: str = "manual-review",
        note: str = "",
    ) -> None:
        if not isinstance(fields, dict) or not fields:
            raise GoldError("gold fields must be a non-empty object")
        if any(not isinstance(key, str) or not key for key in fields):
            raise GoldError("gold field names must be non-empty strings")
        if any(not isinstance(value, str) for value in fields.values()):
            raise GoldError("gold values must be strings")
        if provenance not in GOLD_PROVENANCE:
            raise GoldError("gold provenance must be an explicit human review source")
        if not reviewer or not reviewer.strip():
            raise GoldError("gold needs a named reviewer")
        if not verified_at or not verified_at.strip():
            raise GoldError("gold needs a verification timestamp")
        with self._transaction() as conn:
            capture = conn.execute("SELECT source_tag FROM captures WHERE capture_id = ?", (capture_id,)).fetchone()
            if capture is None:
                raise GoldError("capture %r is not registered" % (capture_id,))
            if capture["source_tag"] == SOURCE_LIVE:
                raise GoldError("live pages are truth-free drift samples and cannot receive gold")
            existing = conn.execute("SELECT fields FROM gold WHERE capture_id = ?", (capture_id,)).fetchone()
            if existing is not None:
                if json.loads(existing["fields"]) != fields:
                    raise GoldError("gold for capture %r is already fixed and differs" % capture_id)
                return
            conn.execute(
                "INSERT INTO gold (capture_id, fields, reviewer, verified_at, provenance, note, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (capture_id, canonical_json(fields), reviewer, verified_at, provenance, note, time.time()),
            )

    def get_gold(self, capture_id: str) -> dict[str, Any] | None:
        with self._read() as conn:
            row = conn.execute("SELECT * FROM gold WHERE capture_id = ?", (capture_id,)).fetchone()
        if row is None:
            return None
        payload = json.loads(row["fields"])
        return {
            "capture_id": capture_id,
            "fields": payload,
            "reviewer": row["reviewer"],
            "verified_at": row["verified_at"],
            "provenance": row["provenance"],
            "note": row["note"],
        }

    # ------------------------------------------------------------------
    # Active rules, activation and circuit state
    # ------------------------------------------------------------------
    def set_initial_active(
        self,
        page_family: str,
        rule_digest: str,
        actor: str,
        note: str,
        circuit_failure_threshold: int = 3,
    ) -> ActiveRule:
        """Install a first active rule for a family that has never been active."""

        package = self.get_rule(rule_digest)
        if package.page_family != page_family:
            raise StoreError("rule belongs to family %s, not %s" % (package.page_family, page_family))
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM active_rules WHERE page_family = ?", (page_family,)).fetchone()
            if row is not None:
                raise WindowError("family %s already has an active rule; use a review window" % page_family)
            if circuit_failure_threshold < 1:
                raise WindowError("circuit_failure_threshold must be at least 1")
            conn.execute(
                "INSERT INTO active_rules (page_family, rule_digest, epoch, activated_at) VALUES (?, ?, 1, ?)",
                (page_family, rule_digest, time.time()),
            )
            conn.execute(
                "INSERT OR REPLACE INTO family_policy (page_family, circuit_failure_threshold) VALUES (?, ?)",
                (page_family, circuit_failure_threshold),
            )
            conn.execute(
                "INSERT INTO activation_events (page_family, from_digest, to_digest, from_epoch, to_epoch, "
                "window_id, actor, note, created_at) VALUES (?, NULL, ?, NULL, 1, NULL, ?, ?, ?)",
                (page_family, rule_digest, actor, note, time.time()),
            )
            row = conn.execute("SELECT * FROM active_rules WHERE page_family = ?", (page_family,)).fetchone()
        return self._active_from_row(row)

    def active_rule(self, page_family: str) -> ActiveRule | None:
        with self._read() as conn:
            row = conn.execute("SELECT * FROM active_rules WHERE page_family = ?", (page_family,)).fetchone()
        return None if row is None else self._active_from_row(row)

    @staticmethod
    def _active_from_row(row: sqlite3.Row) -> ActiveRule:
        return ActiveRule(
            page_family=row["page_family"],
            rule_digest=row["rule_digest"],
            epoch=row["epoch"],
            activated_at=row["activated_at"],
            circuit_state=row["circuit_state"],
            consecutive_failures=row["consecutive_failures"],
            circuit_reason=row["circuit_reason"],
        )

    def activation_events(self, page_family: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM activation_events"
        params: tuple[Any, ...] = ()
        if page_family is not None:
            query += " WHERE page_family = ?"
            params = (page_family,)
        query += " ORDER BY event_id"
        with self._read() as conn:
            return [dict(row) for row in conn.execute(query, params)]

    def circuit_events(self, page_family: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM circuit_events"
        params: tuple[Any, ...] = ()
        if page_family is not None:
            query += " WHERE page_family = ?"
            params = (page_family,)
        query += " ORDER BY event_id"
        with self._read() as conn:
            return [dict(row) for row in conn.execute(query, params)]

    def _open_circuit(self, conn: sqlite3.Connection, family: str, active: sqlite3.Row, reason: str) -> None:
        conn.execute(
            "UPDATE active_rules SET circuit_state = 'open', circuit_opened_at = ?, circuit_reason = ? "
            "WHERE page_family = ?",
            (time.time(), reason, family),
        )
        conn.execute(
            "INSERT INTO circuit_events (page_family, action, rule_digest, consecutive_failures, reason, actor, "
            "window_id, created_at) VALUES (?, 'open', ?, ?, ?, NULL, NULL, ?)",
            (family, active["rule_digest"], active["consecutive_failures"], reason, time.time()),
        )

    def record_production_outcome(
        self,
        capture_id: str,
        page_family: str,
        rule_digest: str,
        status: str,
        produced: bool,
        reason: str,
        failure: bool,
        circuit_failure_threshold: int,
    ) -> ActiveRule:
        """Record one production page and update that family's breaker.

        A failure is any extraction the extractor quarantined (missing
        required field, ambiguity or semantic violation).  Success resets the
        consecutive counter.  When the counter reaches the configured
        threshold the family's circuit opens and production stops emitting
        that family.  No previous rule is activated here; recovery requires a
        reviewed rule revision.
        """

        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM active_rules WHERE page_family = ?", (page_family,)).fetchone()
            if row is None:
                raise WindowError("family %s has no active rule" % page_family)
            if row["rule_digest"] != rule_digest:
                raise StaleWorkerError(
                    "production result was produced by a stale rule",
                    {"expected": row["rule_digest"], "actual": rule_digest},
                )
            page_row = conn.execute(
                "SELECT page_family FROM captures WHERE capture_id = ?", (capture_id,)
            ).fetchone()
            if page_row is None or page_row["page_family"] != page_family:
                raise StaleWorkerError(
                    "production result capture does not belong to family %s" % page_family
                )
            conn.execute(
                "INSERT INTO page_outcomes (capture_id, page_family, rule_digest, status, produced, reason, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (capture_id, page_family, rule_digest, status, 1 if produced else 0, reason, time.time()),
            )
            if failure and row["circuit_state"] == CIRCUIT_CLOSED:
                failures = row["consecutive_failures"] + 1
                conn.execute(
                    "UPDATE active_rules SET consecutive_failures = ?, last_failure_capture = ? WHERE page_family = ?",
                    (failures, capture_id, page_family),
                )
                if failures >= circuit_failure_threshold:
                    active = conn.execute(
                        "SELECT * FROM active_rules WHERE page_family = ?", (page_family,)
                    ).fetchone()
                    self._open_circuit(
                        conn,
                        page_family,
                        active,
                        "%d consecutive semantic failures (threshold %d)"
                        % (failures, circuit_failure_threshold),
                    )
            elif not failure and row["circuit_state"] == CIRCUIT_CLOSED:
                conn.execute(
                    "UPDATE active_rules SET consecutive_failures = 0 WHERE page_family = ?",
                    (page_family,),
                )
            row = conn.execute("SELECT * FROM active_rules WHERE page_family = ?", (page_family,)).fetchone()
        return self._active_from_row(row)

    def circuit_threshold(self, page_family: str) -> int:
        with self._read() as conn:
            row = conn.execute(
                "SELECT circuit_failure_threshold FROM family_policy WHERE page_family = ?", (page_family,)
            ).fetchone()
        return 3 if row is None else row["circuit_failure_threshold"]

    def page_outcomes(self, page_family: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM page_outcomes"
        params: tuple[Any, ...] = ()
        if page_family is not None:
            query += " WHERE page_family = ?"
            params = (page_family,)
        query += " ORDER BY outcome_id"
        with self._read() as conn:
            return [dict(row) for row in conn.execute(query, params)]

    # ------------------------------------------------------------------
    # Review windows: fixed pages, pinned extractor and jobs
    # ------------------------------------------------------------------
    def create_window(
        self,
        candidate_digest: str,
        config_digest: str,
        live_ordinal_start: int,
        live_ordinal_end: int,
        created_by: str,
        note: str,
        extractor_version: str,
        gold_capture_ids: Sequence[str] | dict[str, Sequence[str]] | None = None,
    ) -> dict[str, Any]:
        """Freeze a candidate, thresholds, gold pages and live ordinals.

        The page set is fixed here.  Captures that arrive after this call are
        not silently folded into the review.  Every family in the candidate
        must either have an active rule (the baseline) or be declared as a
        bootstrap family; a review can never "compare" against nothing while
        claiming an old-vs-new result.
        """

        if live_ordinal_start < 0 or live_ordinal_end < live_ordinal_start:
            raise WindowError("invalid live sample ordinal range")
        if not created_by:
            raise WindowError("window needs an author")
        candidate = self.get_candidate(candidate_digest)
        config = self.get_config(config_digest)
        expected_active: dict[str, str | None] = {}
        pages: list[dict[str, Any]] = []
        for rule in sorted(candidate.rules, key=lambda item: item.page_family):
            family = rule.page_family
            active = self.active_rule(family)
            expected_active[family] = None if active is None else active.rule_digest
            with self._read() as conn:
                if gold_capture_ids is None:
                    gold_rows = conn.execute(
                        "SELECT c.capture_id, c.url, c.response_digest, g.fields FROM captures c "
                        "JOIN gold g ON g.capture_id = c.capture_id WHERE c.page_family = ? "
                        "ORDER BY c.captured_at, c.capture_id",
                        (family,),
                    ).fetchall()
                else:
                    gold_rows = []
                    wanted = (
                        gold_capture_ids.get(family, [])
                        if isinstance(gold_capture_ids, dict)
                        else gold_capture_ids
                    )
                    for capture_id in wanted:
                        row = conn.execute(
                            "SELECT c.capture_id, c.url, c.response_digest, c.page_family, g.fields "
                            "FROM captures c JOIN gold g ON g.capture_id = c.capture_id "
                            "WHERE c.capture_id = ?",
                            (capture_id,),
                        ).fetchone()
                        if row is None:
                            raise WindowError("gold page %r is not registered with independent truth" % capture_id)
                        if row["page_family"] != family:
                            raise WindowError(
                                "gold page %r belongs to family %s" % (capture_id, row["page_family"])
                            )
                        gold_rows.append(row)
                live_rows = conn.execute(
                    "SELECT capture_id, url, response_digest, sample_ordinal FROM captures "
                    "WHERE page_family = ? AND source_tag = 'live' AND sample_ordinal IS NOT NULL "
                    "AND sample_ordinal BETWEEN ? AND ? ORDER BY sample_ordinal, captured_at, capture_id",
                    (family, live_ordinal_start, live_ordinal_end),
                ).fetchall()
            for row in gold_rows:
                pages.append(
                    {
                        "capture_id": row["capture_id"],
                        "page_family": family,
                        "split": "gold",
                        "sample_ordinal": None,
                        "response_digest": row["response_digest"],
                        "gold_fields": json.loads(row["fields"]),
                    }
                )
            seen = {row["capture_id"] for row in gold_rows}
            for row in live_rows:
                if row["capture_id"] in seen:
                    continue
                pages.append(
                    {
                        "capture_id": row["capture_id"],
                        "page_family": family,
                        "split": "live",
                        "sample_ordinal": row["sample_ordinal"],
                        "response_digest": row["response_digest"],
                        "gold_fields": None,
                    }
                )
            if config.for_family(family) is None:
                raise WindowError("release config has no thresholds for family %s" % family)
            if active is None:
                # A review is a comparison.  A brand-new family is bootstrapped
                # through set_initial_active, never smuggled through a window
                # that claims an old-vs-new result.
                raise WindowError(
                    "family %s has no active baseline; install one explicitly first" % family
                )
        if not pages:
            raise WindowError("window contains no pages")
        window_id = uuid.uuid4().hex
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO windows (window_id, candidate_digest, config_digest, expected_active, "
                "extractor_version, live_ordinal_start, live_ordinal_end, status, created_by, note, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)",
                (
                    window_id,
                    candidate_digest,
                    config_digest,
                    canonical_json(expected_active),
                    extractor_version,
                    live_ordinal_start,
                    live_ordinal_end,
                    created_by,
                    note,
                    time.time(),
                ),
            )
            for page in pages:
                conn.execute(
                    "INSERT INTO window_pages (window_id, capture_id, page_family, split, sample_ordinal, "
                    "response_digest, gold_snapshot) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        window_id,
                        page["capture_id"],
                        page["page_family"],
                        page["split"],
                        page["sample_ordinal"],
                        page["response_digest"],
                        None if page["gold_fields"] is None else canonical_json(page["gold_fields"]),
                    ),
                )
                rule = candidate.rule_for(page["page_family"])
                conn.execute(
                    "INSERT INTO shadow_jobs (job_id, window_id, capture_id, page_family, rule_digest, "
                    "baseline_rule_digest, extractor_version, status, fence) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0)",
                    (
                        uuid.uuid4().hex,
                        window_id,
                        page["capture_id"],
                        page["page_family"],
                        rule.digest,
                        expected_active[page["page_family"]],
                        extractor_version,
                    ),
                )
        return self.get_window(window_id)

    def get_window(self, window_id: str) -> dict[str, Any]:
        with self._read() as conn:
            row = conn.execute("SELECT * FROM windows WHERE window_id = ?", (window_id,)).fetchone()
            if row is None:
                raise WindowError("window %r is not registered" % (window_id,))
            pages = conn.execute(
                "SELECT wp.*, c.url FROM window_pages wp JOIN captures c ON c.capture_id = wp.capture_id "
                "WHERE wp.window_id = ? ORDER BY wp.page_family, wp.split, wp.sample_ordinal, wp.capture_id",
                (window_id,),
            ).fetchall()
            jobs = conn.execute(
                "SELECT * FROM shadow_jobs WHERE window_id = ? ORDER BY page_family, job_id", (window_id,)
            ).fetchall()
            evaluation = conn.execute(
                "SELECT decision, canonical, created_at FROM evaluations WHERE window_id = ?", (window_id,)
            ).fetchone()
        window = dict(row)
        window["expected_active"] = json.loads(window["expected_active"])
        window["pages"] = [dict(page) for page in pages]
        window["jobs"] = [dict(job) for job in jobs]
        window["evaluation"] = None if evaluation is None else {
            "decision": evaluation["decision"],
            "created_at": evaluation["created_at"],
            "report": json.loads(evaluation["canonical"]),
        }
        return window

    def list_windows(self) -> list[dict[str, Any]]:
        with self._read() as conn:
            rows = conn.execute("SELECT window_id, status, decision, created_at FROM windows ORDER BY created_at").fetchall()
        return [dict(row) for row in rows]

    def _window_is_open(self, conn: sqlite3.Connection, window_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM windows WHERE window_id = ?", (window_id,)).fetchone()
        if row is None:
            raise WindowError("window %r is not registered" % (window_id,))
        if row["status"] not in (WINDOW_OPEN, WINDOW_EVALUATED):
            raise WindowError("window is %s and cannot accept work" % row["status"])
        return row

    def lease_jobs(
        self,
        window_id: str,
        worker_id: str,
        extractor_version: str,
        limit: int = 10,
        lease_seconds: float = 300.0,
    ) -> list[ShadowJob]:
        """Lease pending jobs to a worker with a pinned extractor version."""

        if not worker_id:
            raise WindowError("worker id is required")
        if limit < 1:
            raise WindowError("limit must be positive")
        now = time.time()
        leased: list[ShadowJob] = []
        with self._transaction() as conn:
            window = self._window_is_open(conn, window_id)
            if window["status"] != WINDOW_OPEN:
                raise WindowError("window is already evaluated")
            if window["extractor_version"] != extractor_version:
                raise WindowError(
                    "worker extractor %s does not match the window's pinned %s"
                    % (extractor_version, window["extractor_version"])
                )
            rows = conn.execute(
                "SELECT * FROM shadow_jobs WHERE window_id = ? AND extractor_version = ? AND "
                "(status = 'pending' OR (status = 'leased' AND (lease_expires IS NULL OR lease_expires < ?))) "
                "ORDER BY job_id LIMIT ?",
                (window_id, extractor_version, now, limit),
            ).fetchall()
            for row in rows:
                token = uuid.uuid4().hex
                fence = row["fence"] + 1
                conn.execute(
                    "UPDATE shadow_jobs SET status = 'leased', fence = ?, lease_token = ?, worker_id = ?, "
                    "leased_at = ?, lease_expires = ?, attempts = attempts + 1 WHERE job_id = ?",
                    (fence, token, worker_id, now, now + lease_seconds, row["job_id"]),
                )
                leased_row = conn.execute("SELECT * FROM shadow_jobs WHERE job_id = ?", (row["job_id"],)).fetchone()
                leased.append(self._job_from_row(leased_row, lease_token=token))
        return leased

    def _job_from_row(self, row: sqlite3.Row, lease_token: str | None = None) -> ShadowJob:
        capture = self.get_capture(row["capture_id"])
        return ShadowJob(
            job_id=row["job_id"],
            window_id=row["window_id"],
            capture_id=row["capture_id"],
            page_family=row["page_family"],
            rule_digest=row["rule_digest"],
            baseline_rule_digest=row["baseline_rule_digest"],
            extractor_version=row["extractor_version"],
            status=row["status"],
            fence=row["fence"],
            lease_token=lease_token,
            worker_id=row["worker_id"],
            capture=capture,
        )

    @staticmethod
    def _reject(
        conn: sqlite3.Connection,
        job_row: sqlite3.Row | None,
        reason: str,
        payload: Any,
        worker_id: str | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO rejected_results (job_id, window_id, worker_id, rule_digest, extractor_version, "
            "reason, payload, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                None if job_row is None else job_row["job_id"],
                None if job_row is None else job_row["window_id"],
                worker_id,
                None if job_row is None else job_row["rule_digest"],
                None if job_row is None else job_row["extractor_version"],
                reason,
                canonical_json(payload),
                time.time(),
            ),
        )

    def complete_job(
        self,
        job_id: str,
        lease_token: str | None,
        fence: int,
        worker_id: str,
        payload: dict[str, Any],
    ) -> None:
        """Accept a shadow result only from the current lease and version.

        Rejections are logged in their own transaction after the failed
        completion transaction rolls back.  Otherwise the audit trail would
        disappear together with the rejected write, and an old worker's
        mislabelled output would leave no trace.
        """

        try:
            self._complete_job(job_id, lease_token, fence, worker_id, payload)
        except _JobRejection as rejection:
            with self._transaction() as conn:
                self._reject(conn, rejection.job_row, rejection.reason, payload, worker_id)
            raise StaleWorkerError(rejection.reason, rejection.detail) from None

    def _complete_job(
        self,
        job_id: str,
        lease_token: str | None,
        fence: int,
        worker_id: str,
        payload: dict[str, Any],
    ) -> None:
        def reject(row, reason, detail=None):
            raise _JobRejection(reason, row, detail or {})

        now = time.time()
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM shadow_jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                reject(None, "unknown job %r" % (job_id,))
            if row["status"] == JOB_DONE:
                reject(row, "duplicate completion: job already completed")
            if row["status"] == JOB_SUPERSEDED:
                reject(row, "job was superseded")
            if row["status"] != JOB_LEASED:
                reject(row, "job is not leased")
            if not lease_token or lease_token != row["lease_token"] or int(fence) != row["fence"]:
                reject(
                    row,
                    "lease token/fence mismatch",
                    {"expected_fence": row["fence"], "actual_fence": fence},
                )
            if row["lease_expires"] is not None and row["lease_expires"] < now:
                reject(row, "lease expired")
            if worker_id != row["worker_id"]:
                reject(row, "worker id does not own the lease")
            window = conn.execute("SELECT * FROM windows WHERE window_id = ?", (row["window_id"],)).fetchone()
            if window is None or window["status"] != WINDOW_OPEN:
                reject(
                    row,
                    "window is no longer open (status %s)" % (None if window is None else window["status"]),
                )
            if payload.get("rule_digest") != row["rule_digest"]:
                reject(row, "result rule digest does not match the job")
            if payload.get("extractor_version") != row["extractor_version"]:
                reject(row, "result extractor version does not match the job")
            if payload.get("capture_id") != row["capture_id"]:
                reject(row, "result is for a different capture")
            capture = conn.execute("SELECT * FROM captures WHERE capture_id = ?", (row["capture_id"],)).fetchone()
            if capture is None or sha256_bytes(bytes(capture["body"])) != payload.get("response_digest"):
                reject(row, "result response digest does not match captured bytes")
            if payload.get("page_family") != row["page_family"]:
                reject(row, "result page family does not match the job")
            submitted_family = payload.get("page_family")
            page_row = conn.execute(
                "SELECT page_family FROM window_pages WHERE window_id = ? AND capture_id = ?",
                (row["window_id"], row["capture_id"]),
            ).fetchone()
            if page_row is None or page_row["page_family"] != submitted_family:
                reject(row, "result does not correspond to a frozen window page")
            conn.execute(
                "INSERT INTO shadow_results (job_id, window_id, capture_id, page_family, rule_digest, "
                "extractor_version, response_digest, result, submitted_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    row["window_id"],
                    row["capture_id"],
                    row["page_family"],
                    row["rule_digest"],
                    row["extractor_version"],
                    payload["response_digest"],
                    canonical_json(payload),
                    now,
                ),
            )
            conn.execute(
                "UPDATE shadow_jobs SET status = 'done', completed_at = ?, lease_token = NULL WHERE job_id = ?",
                (now, job_id),
            )

    def record_production_result(
        self,
        capture_id: str,
        page_family: str,
        rule_digest: str,
        status: str,
        result: dict[str, Any],
    ) -> None:
        """Persist a production extraction produced by the active version.

        Production output is never derived from a shadow/candidate result.
        The caller must pass the active rule digest that actually ran; a
        stale worker is rejected by :class:`ProductionRunner` before this
        point.
        """

        with self._transaction() as conn:
            row = conn.execute("SELECT rule_digest FROM active_rules WHERE page_family = ?", (page_family,)).fetchone()
            if row is None:
                raise WindowError("family %s has no active rule" % page_family)
            if row["rule_digest"] != rule_digest:
                raise StaleWorkerError("production result came from a non-active rule")
            conn.execute(
                "INSERT INTO production_results (capture_id, page_family, rule_digest, status, result, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (capture_id, page_family, rule_digest, status, canonical_json(result), time.time()),
            )

    def production_results(self, page_family: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM production_results"
        params: tuple[Any, ...] = ()
        if page_family is not None:
            query += " WHERE page_family = ?"
            params = (page_family,)
        query += " ORDER BY result_id"
        with self._read() as conn:
            return [dict(row) for row in conn.execute(query, params)]

    def rejected_results(self, window_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM rejected_results"
        params: tuple[Any, ...] = ()
        if window_id is not None:
            query += " WHERE window_id = ?"
            params = (window_id,)
        query += " ORDER BY rejection_id"
        with self._read() as conn:
            return [dict(row) for row in conn.execute(query, params)]

    # ------------------------------------------------------------------
    # Reporting, evaluation and fusion-safe activation
    # ------------------------------------------------------------------
    def shadow_results(self, window_id: str) -> list[dict[str, Any]]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT * FROM shadow_results WHERE window_id = ? ORDER BY page_family, capture_id",
                (window_id,),
            ).fetchall()
        results = []
        for row in rows:
            payload = json.loads(row["result"])
            results.append({"job_id": row["job_id"], "payload": payload})
        return results

    def set_evaluation(self, window_id: str, report: dict[str, Any], decision: str) -> None:
        if decision not in {"pass", "hold"}:
            raise WindowError("unknown decision %r" % (decision,))
        canonical = canonical_json(report)
        with self._transaction() as conn:
            window = conn.execute("SELECT status FROM windows WHERE window_id = ?", (window_id,)).fetchone()
            if window is None:
                raise WindowError("window %r is not registered" % (window_id,))
            if window["status"] not in (WINDOW_OPEN, WINDOW_EVALUATED):
                raise WindowError("window is %s and cannot be evaluated" % window["status"])
            conn.execute("DELETE FROM evaluations WHERE window_id = ?", (window_id,))
            conn.execute(
                "INSERT INTO evaluations (window_id, decision, canonical, created_at) VALUES (?, ?, ?, ?)",
                (window_id, decision, canonical, time.time()),
            )
            conn.execute(
                "UPDATE windows SET status = 'evaluated', decision = ?, evaluated_at = ? WHERE window_id = ?",
                (decision, time.time(), window_id),
            )

    def mark_window_stale(self, window_id: str, reason: str) -> None:
        with self._transaction() as conn:
            row = conn.execute("SELECT status FROM windows WHERE window_id = ?", (window_id,)).fetchone()
            if row is None:
                raise WindowError("window %r is not registered" % (window_id,))
            if row["status"] == WINDOW_ACTIVATED:
                raise WindowError("an activated window cannot be retroactively marked stale")
            conn.execute(
                "UPDATE windows SET status = 'stale', stale_reason = ?, invalidated_at = ? WHERE window_id = ?",
                (reason, time.time(), window_id),
            )

    def window_baseline_changes(self, window_id: str) -> dict[str, dict[str, Any]]:
        """Return families whose active rule changed since the window froze."""

        window = self.get_window(window_id)
        changes: dict[str, dict[str, Any]] = {}
        for family, expected in window["expected_active"].items():
            active = self.active_rule(family)
            actual = None if active is None else active.rule_digest
            if actual != expected:
                changes[family] = {"expected": expected, "actual": actual}
        return changes

    def activate_window(self, window_id: str, actor: str, note: str) -> dict[str, Any]:
        """Replace active rules only if every baseline still matches.

        This is a compare-and-swap per family inside one transaction.  If any
        family's active digest moved, the whole window is marked stale and no
        family is activated.  The window's evaluated rule digests are also
        re-checked against the candidate, so an edited or re-pointed package
        can never be activated under an old review.
        """

        if not actor:
            raise WindowError("activation needs an actor")
        now = time.time()
        stale_reason = None
        activation: dict[str, Any] = {}
        with self._transaction() as conn:
            window = conn.execute("SELECT * FROM windows WHERE window_id = ?", (window_id,)).fetchone()
            if window is None:
                raise WindowError("window %r is not registered" % (window_id,))
            if window["status"] == WINDOW_ACTIVATED:
                raise WindowError("window is already activated")
            if window["status"] == WINDOW_STALE:
                raise WindowError("window is stale; run a new review")
            if window["status"] != WINDOW_EVALUATED:
                raise WindowError("window must be evaluated before activation")
            evaluation = conn.execute(
                "SELECT decision FROM evaluations WHERE window_id = ?", (window_id,)
            ).fetchone()
            if evaluation is None or evaluation["decision"] != "pass":
                raise WindowError("review did not pass; active rules are unchanged")
            candidate = self.get_candidate(window["candidate_digest"])
            config = self.get_config(window["config_digest"])
            expected_active = json.loads(window["expected_active"])
            if set(expected_active) != {rule.page_family for rule in candidate.rules}:
                raise WindowError("window baseline set does not match the candidate")
            changes: dict[str, dict[str, Any]] = {}
            for family, expected in expected_active.items():
                row = conn.execute("SELECT * FROM active_rules WHERE page_family = ?", (family,)).fetchone()
                actual = None if row is None else row["rule_digest"]
                if actual != expected:
                    changes[family] = {"expected": expected, "actual": actual}
            if changes:
                stale_reason = "active rule changed during review: %s" % canonical_json(changes)
                conn.execute(
                    "UPDATE windows SET status = 'stale', stale_reason = ?, invalidated_at = ? WHERE window_id = ?",
                    (stale_reason, now, window_id),
                )
            for rule in ([] if stale_reason else candidate.rules):
                stored = conn.execute(
                    "SELECT digest FROM rule_packages WHERE digest = ?", (rule.digest,)
                ).fetchone()
                if stored is None:
                    raise UnknownRuleError("candidate rule disappeared from the store")
            if not stale_reason:
                for rule in candidate.rules:
                    row = conn.execute(
                        "SELECT * FROM active_rules WHERE page_family = ?", (rule.page_family,)
                    ).fetchone()
                    opened_digest = conn.execute(
                        "SELECT rule_digest FROM circuit_events WHERE page_family = ? AND action = 'open' "
                        "ORDER BY event_id DESC LIMIT 1",
                        (rule.page_family,),
                    ).fetchone()
                    if row["circuit_state"] == "open" and (
                        opened_digest is not None and opened_digest["rule_digest"] == rule.digest
                    ):
                        stale_reason = (
                            "family %s candidate is the rule that opened the breaker; "
                            "revise the rule first" % rule.page_family
                        )
                        break
            if stale_reason:
                conn.execute(
                    "UPDATE windows SET status = 'stale', stale_reason = ?, invalidated_at = ? WHERE window_id = ?",
                    (stale_reason, now, window_id),
                )
            for rule in ([] if stale_reason else candidate.rules):
                row = conn.execute(
                    "SELECT * FROM active_rules WHERE page_family = ?", (rule.page_family,)
                ).fetchone()
                new_epoch = row["epoch"] + 1
                if row["circuit_state"] == "closed":
                    # A newly activated revision starts with a clean failure
                    # streak; failures of the previous rule must not count
                    # against it.
                    conn.execute(
                        "UPDATE active_rules SET rule_digest = ?, epoch = ?, activated_at = ?, "
                        "consecutive_failures = 0, last_failure_capture = NULL WHERE page_family = ?",
                        (rule.digest, new_epoch, now, rule.page_family),
                    )
                else:
                    # A genuinely new reviewed revision replaces the broken
                    # rule and closes the breaker; a failure reopens it.
                    conn.execute(
                        "UPDATE active_rules SET rule_digest = ?, epoch = ?, activated_at = ?, "
                        "circuit_state = 'closed', consecutive_failures = 0, circuit_opened_at = NULL, "
                        "circuit_reason = NULL WHERE page_family = ?",
                        (rule.digest, new_epoch, now, rule.page_family),
                    )
                conn.execute(
                    "INSERT INTO activation_events (page_family, from_digest, to_digest, from_epoch, to_epoch, "
                    "window_id, actor, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        rule.page_family,
                        row["rule_digest"],
                        rule.digest,
                        row["epoch"],
                        new_epoch,
                        window_id,
                        actor,
                        note,
                        now,
                    ),
                )
                activation[rule.page_family] = {
                    "from_digest": row["rule_digest"],
                    "to_digest": rule.digest,
                    "from_epoch": row["epoch"],
                    "to_epoch": new_epoch,
                }
            if not stale_reason:
                for rule in candidate.rules:
                    conn.execute(
                        "INSERT OR REPLACE INTO family_policy (page_family, circuit_failure_threshold) VALUES (?, ?)",
                        (rule.page_family, config.circuit_failure_threshold),
                    )
                conn.execute(
                    "UPDATE windows SET status = 'activated', activated_at = ? WHERE window_id = ?",
                    (now, window_id),
                )
        if stale_reason:
            raise WindowError("%s; review again" % stale_reason)
        return {"window_id": window_id, "activated": activation, "actor": actor, "note": note}
