"""Shadow review: compare candidate rules against the frozen baseline.

The report is deliberately page-oriented.  Every family gets rates for the
dashboard, but every rate is backed by the individual pages that produced
it, including the capture id, URL, response digest, candidate field status,
matched node evidence and, for reviewed pages only, independent gold.

Rules of the review
-------------------
* Gold is the *only* truth used for conflicts.  Baseline extraction values are
  never promoted to gold, and live pages have no gold.
* Live pages are drift hints.  They can fail structural checks, but the report
  never says the candidate is "more correct" on a truth-free page.
* A family must meet its own sample floor and thresholds.  Good results in
  another family cannot compensate.
* The baseline is re-extracted at review time from the captured bytes using
  the window's pinned extractor.  It is a comparison baseline, not stored
  production output.
"""
from __future__ import annotations

from typing import Any
import json

from .extractor import EXTRACTOR_VERSION, Extractor, PAGE_OK
from .rules import RulePackage
from .store import Store, WindowError, canonical_json

REPORT_SCHEMA = "catalog.shadow-review/v1"
DECISION_PASS = "pass"
DECISION_HOLD = "hold"


class ReviewError(RuntimeError):
    """The window cannot be evaluated without lying about its inputs."""


def _values(extraction: dict[str, Any]) -> dict[str, str]:
    """Raw selected values, including values that then failed a constraint.

    Showing the quarantined selection (for example ``推荐机构``) is important:
    it is the difference reviewers need to see.  The field status is compared
    separately, so a quarantined value is never mistaken for accepted output.
    """

    return {
        name: result["value"]
        for name, result in extraction.get("fields", {}).items()
        if result.get("value") is not None
    }


def _gold_conflicts(extraction: dict[str, Any], gold_fields: dict[str, str]) -> list[dict[str, Any]]:
    """Return every reviewed field the candidate failed to reproduce.

    Missing and ambiguous outputs are conflicts too: the page was reviewed
    and the rule failed to reproduce a known value.  They are not silently
    skipped from the conflict count.
    """

    conflicts: list[dict[str, Any]] = []
    fields = extraction.get("fields", {})
    for name, expected in sorted(gold_fields.items()):
        result = fields.get(name)
        if result is None:
            conflicts.append({"field": name, "expected": expected, "actual": None, "reason": "field not in output"})
            continue
        if result.get("status") != "ok":
            conflicts.append(
                {
                    "field": name,
                    "expected": expected,
                    "actual": None,
                    "reason": "field status %s" % result.get("status"),
                    "evidence": result.get("candidates", []),
                }
            )
            continue
        actual = result.get("value")
        if actual != expected:
            conflicts.append(
                {
                    "field": name,
                    "expected": expected,
                    "actual": actual,
                    "reason": "value differs",
                    "evidence": result.get("candidates", []),
                }
            )
    return conflicts


def _differences(candidate: dict[str, Any], baseline: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if candidate.get("status") != baseline.get("status"):
        reasons.append("status %s -> %s" % (baseline.get("status"), candidate.get("status")))
    candidate_values = _values(candidate)
    baseline_values = _values(baseline)
    for name in sorted(set(candidate_values) | set(baseline_values)):
        if candidate_values.get(name) != baseline_values.get(name):
            reasons.append(
                "field %s: %r -> %r" % (name, baseline_values.get(name), candidate_values.get(name))
            )
    return reasons


def _family_report(
    family: str,
    pages: list[dict[str, Any]],
    thresholds: Any,
    results: dict[str, dict[str, Any]],
    required_fields: list[str],
) -> dict[str, Any]:
    reasons: list[str] = []
    required_total = 0
    required_passed = 0
    ambiguous_pages = 0
    conflict_pages: set[str] = set()
    conflicts_total = 0
    diff_pages = 0
    missing_results = 0
    page_reports: list[dict[str, Any]] = []

    for page in pages:
        capture_id = page["capture_id"]
        candidate = results.get(capture_id)
        entry: dict[str, Any] = {
            "capture_id": capture_id,
            "url": page["url"],
            "split": page["split"],
            "sample_ordinal": page["sample_ordinal"],
            "response_digest": page["response_digest"],
            "candidate": None,
            "baseline": page["baseline"],
            "gold": page["gold"],
            "gold_conflicts": [],
            "baseline_gold_conflicts": [],
            "diff": {"differs": False, "reasons": []},
            "drift_hint": page["split"] == "live",
        }
        if candidate is None:
            missing_results += 1
            required_total += len(required_fields)
            entry["error"] = "shadow result missing"
            entry["diff"] = {"differs": False, "reasons": ["shadow result missing"]}
            page_reports.append(entry)
            continue
        entry["candidate"] = {
            "status": candidate.get("status"),
            "reasons": candidate.get("reasons", []),
            "fields": candidate.get("fields", {}),
            "resource_scope": candidate.get("resource_scope"),
        }
        fields = candidate.get("fields", {})
        for name, field in fields.items():
            if not field.get("required"):
                continue
            required_total += 1
            if field.get("status") == "ok":
                required_passed += 1
        if any(field.get("status") == "ambiguous" for field in fields.values()):
            ambiguous_pages += 1
        conflicts = _gold_conflicts(candidate, page["gold"]["fields"]) if page["gold"] else []
        entry["gold_conflicts"] = conflicts
        if page["gold"]:
            entry["baseline_gold_conflicts"] = _gold_conflicts(page["baseline"], page["gold"]["fields"])
        if conflicts:
            conflict_pages.add(capture_id)
            conflicts_total += len(conflicts)
        diffs = _differences(candidate, page["baseline"])
        entry["diff"] = {"differs": bool(diffs), "reasons": diffs}
        if diffs:
            diff_pages += 1
        page_reports.append(entry)

    pages_total = len(pages)
    gold_pages = sum(1 for page in pages if page["gold"] is not None)
    live_pages = pages_total - gold_pages
    required_rate = (required_passed / required_total) if required_total else 0.0
    ambiguity_rate = (ambiguous_pages / pages_total) if pages_total else 1.0
    diff_rate = (diff_pages / pages_total) if pages_total else 1.0
    metrics = {
        "pages_total": pages_total,
        "gold_pages": gold_pages,
        "live_pages": live_pages,
        "results_present": pages_total - missing_results,
        "missing_results": missing_results,
        "required_fields_total": required_total,
        "required_fields_passed": required_passed,
        "required_pass_rate": required_rate,
        "page_pass_rate": (
            sum(1 for entry in page_reports if entry.get("candidate") and entry["candidate"]["status"] == PAGE_OK)
            / pages_total
            if pages_total
            else 0.0
        ),
        "ambiguous_pages": ambiguous_pages,
        "ambiguity_rate": ambiguity_rate,
        "gold_conflict_pages": len(conflict_pages),
        "gold_conflicts": conflicts_total,
        "diff_pages": diff_pages,
        "diff_rate": diff_rate,
    }
    if missing_results:
        reasons.append("%d page(s) have no shadow result" % missing_results)
    if pages_total < thresholds.min_pages:
        reasons.append(
            "only %d page(s), minimum for this family is %d" % (pages_total, thresholds.min_pages)
        )
    if live_pages < thresholds.min_live_pages:
        reasons.append(
            "only %d live page(s), minimum for this family is %d" % (live_pages, thresholds.min_live_pages)
        )
    if gold_pages < thresholds.min_gold_pages:
        reasons.append(
            "only %d gold page(s), minimum for this family is %d" % (gold_pages, thresholds.min_gold_pages)
        )
    if required_rate < thresholds.required_pass_rate:
        reasons.append(
            "required pass rate %.4f is below %.4f" % (required_rate, thresholds.required_pass_rate)
        )
    if ambiguity_rate > thresholds.max_ambiguity_rate:
        reasons.append("ambiguity rate %.4f exceeds %.4f" % (ambiguity_rate, thresholds.max_ambiguity_rate))
    if conflicts_total > thresholds.max_gold_conflicts:
        reasons.append(
            "%d gold conflict(s) exceed the maximum of %d" % (conflicts_total, thresholds.max_gold_conflicts)
        )
    if thresholds.max_diff_rate is not None and diff_rate > thresholds.max_diff_rate:
        reasons.append("diff rate %.4f exceeds %.4f" % (diff_rate, thresholds.max_diff_rate))
    return {
        "family": family,
        "passed": not reasons,
        "reasons": reasons,
        "thresholds": thresholds.to_json(),
        "metrics": metrics,
        "pages": page_reports,
    }


def _baseline_extraction(
    package: RulePackage | None,
    capture: Any,
    extractor_version: str,
) -> dict[str, Any]:
    if package is None:
        return {
            "status": "no_baseline",
            "extractor_version": extractor_version,
            "fields": {},
            "reasons": ["family had no active rule when the window was frozen"],
        }
    extraction = Extractor(package).extract(capture.body, capture.url, capture.response_digest)
    payload = extraction.to_json()
    payload["extractor_version"] = extractor_version
    return payload


def evaluate_window(store: Store, window_id: str) -> dict[str, Any]:
    """Evaluate a frozen window and persist the page-level report.

    The caller must have already completed the shadow jobs.  Re-evaluation is
    allowed while the window is open or evaluated; activation is what freezes
    the decision.
    """

    window = store.get_window(window_id)
    if window["status"] in ("activated", "stale"):
        raise WindowError("window is %s and cannot be evaluated again" % window["status"])
    if window["extractor_version"] != EXTRACTOR_VERSION:
        raise ReviewError(
            "window pins extractor %s but this evaluator is %s"
            % (window["extractor_version"], EXTRACTOR_VERSION)
        )

    candidate = store.get_candidate(window["candidate_digest"])
    config = store.get_config(window["config_digest"])
    baseline_changes = store.window_baseline_changes(window_id)

    raw_results = {item["job_id"]: item["payload"] for item in store.shadow_results(window_id)}
    result_by_capture: dict[str, dict[str, Any]] = {}
    for job_id, payload in raw_results.items():
        job = next((item for item in window["jobs"] if item["job_id"] == job_id), None)
        if job is None:
            continue
        if payload.get("rule_digest") != job["rule_digest"]:
            raise ReviewError("stored shadow result rule digest does not match its job")
        if payload.get("extractor_version") != window["extractor_version"]:
            raise ReviewError("stored shadow result extractor version does not match the window")
        if payload.get("capture_id") != job["capture_id"]:
            raise ReviewError("stored shadow result capture does not match its job")
        capture = store.get_capture(job["capture_id"])
        if payload.get("response_digest") != capture.response_digest:
            raise ReviewError("stored shadow result does not match the captured bytes")
        expected = Extractor(store.get_rule(job["rule_digest"])).extract(
            capture.body, capture.url, capture.response_digest
        )
        expected_payload = expected.to_json()
        for key in ("status", "fields", "resource_scope", "rule_digest", "extractor_version"):
            if payload.get(key) != expected_payload.get(key):
                raise ReviewError(
                    "stored shadow result field %r does not reproduce from captured bytes" % key
                )
        result_by_capture[job["capture_id"]] = payload

    for job in window["jobs"]:
        expected_digest = candidate.rule_for(job["page_family"]).digest
        if job["rule_digest"] != expected_digest:
            raise ReviewError(
                "job %s was created for a different rule digest than the candidate" % job["job_id"]
            )

    pages_by_family: dict[str, list[dict[str, Any]]] = {family: [] for family in candidate.rule_digests}
    for page in window["pages"]:
        capture = store.get_capture(page["capture_id"])
        if capture.response_digest != page["response_digest"]:
            raise ReviewError("capture bytes changed after the window was frozen")
        snapshot = json.loads(page["gold_snapshot"]) if page.get("gold_snapshot") else None
        current = store.get_gold(page["capture_id"])
        if page["split"] == "gold":
            if snapshot is None or current is None:
                raise ReviewError("gold page %s has no independent truth" % page["capture_id"])
            if current["fields"] != snapshot:
                raise ReviewError("gold for %s changed after the window was frozen" % page["capture_id"])
            gold = current
        else:
            if current is not None:
                raise ReviewError(
                    "live page %s carries gold; live samples must stay truth-free" % page["capture_id"]
                )
            gold = None
        family = page["page_family"]
        baseline_digest = window["expected_active"].get(family)
        baseline_package = store.get_rule(baseline_digest) if baseline_digest else None
        baseline = _baseline_extraction(baseline_package, capture, window["extractor_version"])
        pages_by_family.setdefault(family, []).append(
            {
                "capture_id": page["capture_id"],
                "url": page["url"],
                "split": page["split"],
                "sample_ordinal": page["sample_ordinal"],
                "response_digest": page["response_digest"],
                "gold": gold,
                "baseline": baseline,
            }
        )

    families: dict[str, Any] = {}
    for family in sorted(candidate.rule_digests):
        thresholds = config.for_family(family)
        if thresholds is None:
            raise ReviewError("release config has no thresholds for family %s" % family)
        family_pages = pages_by_family.get(family, [])
        family_rule = candidate.rule_for(family)
        required_fields = [field.name for field in family_rule.required_fields()]
        family_report = _family_report(
            family, family_pages, thresholds, result_by_capture, required_fields
        )
        families[family] = family_report

    if baseline_changes:
        report = {
            "schema": REPORT_SCHEMA,
            "window_id": window_id,
            "candidate_label": candidate.label,
            "candidate_digest": window["candidate_digest"],
            "config_digest": window["config_digest"],
            "extractor_version": window["extractor_version"],
            "decision": DECISION_HOLD,
            "decision_reasons": ["active rule changed while the window was open; review again"],
            "baseline_changes": baseline_changes,
            "families": families,
            "summary": {
                "families": len(families),
                "passed": sum(1 for item in families.values() if item["passed"]),
                "held": sum(1 for item in families.values() if not item["passed"]),
            },
        }
        store.mark_window_stale(window_id, canonical_json(baseline_changes))
        return report

    decision_reasons = []
    for family, family_report in families.items():
        decision_reasons.extend("%s: %s" % (family, reason) for reason in family_report["reasons"])
    decision = DECISION_PASS if not decision_reasons else DECISION_HOLD
    report = {
        "schema": REPORT_SCHEMA,
        "window_id": window_id,
        "candidate_label": candidate.label,
        "candidate_digest": window["candidate_digest"],
        "config_digest": window["config_digest"],
        "extractor_version": window["extractor_version"],
        "decision": decision,
        "decision_reasons": decision_reasons,
        "baseline_changes": {},
        "families": families,
        "summary": {
            "families": len(families),
            "passed": sum(1 for item in families.values() if item["passed"]),
            "held": sum(1 for item in families.values() if not item["passed"]),
        },
    }
    store.set_evaluation(window_id, report, decision)
    return report


__all__ = [
    "DECISION_HOLD",
    "DECISION_PASS",
    "REPORT_SCHEMA",
    "ReviewError",
    "evaluate_window",
]
