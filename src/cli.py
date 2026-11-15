"""Command-line drills for the rollout mechanism.

Subcommands mirror the operating procedure:

``seed``       register immutable rules, install a baseline, capture pages,
               attach independent gold, and freeze a review window.
``review``     run pinned shadow workers, evaluate the window and print the
               page-level decision.
``activate``   compare-and-swap activation after a passing review.
``production`` run the active rule over live pages and exercise the breaker.
``show``       print the stored window/evaluation for audit.

All data stays in a local SQLite file.  The drill service is only reachable
on the loopback address.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .collector import SupplierClient, capture_page
from .extractor import EXTRACTOR_VERSION
from .review import evaluate_window
from .rules import ReleaseCandidate, ReleaseConfig, RulePackage
from .store import Store
from .workers import ProductionRunner, run_shadow

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def open_store(path: str) -> Store:
    return Store(path)


def cmd_seed(args) -> int:
    rules = load("rules.json")
    source = load("source.json")
    store = open_store(args.database)
    baseline = RulePackage.from_json(rules["baseline"][0])
    candidate_rule = RulePackage.from_json(rules["candidate"][0])
    store.register_rule(baseline)
    store.register_rule(candidate_rule)
    store.set_initial_active(
        baseline.page_family,
        baseline.digest,
        args.actor,
        "initial baseline",
        circuit_failure_threshold=config.circuit_failure_threshold,
    )
    candidate = ReleaseCandidate(args.candidate_label, (candidate_rule,))
    store.register_candidate(candidate)
    # fixtures/release.json is the file operators edit to change thresholds.
    # fixtures/source.json's ``release`` block is only the supplier-protocol
    # example consumed by tests/test_source.py.
    config = ReleaseConfig.from_json(load("release.json"))
    store.register_config(config)

    client = SupplierClient(args.base_url)
    series_baseline = RulePackage.from_json(rules["series_baseline"][0])
    store.register_rule(series_baseline)
    if store.active_rule(series_baseline.page_family) is None:
        store.set_initial_active(
            series_baseline.page_family,
            series_baseline.digest,
            args.actor,
            "initial series baseline",
            circuit_failure_threshold=config.circuit_failure_threshold,
        )
    for name in ("series-old", "series-old2"):
        capture = capture_page(store, client, name, series_baseline.page_family, "archive")
        store.set_gold(
            capture.capture_id,
            source["gold"][name],
            args.actor,
            args.verified_at,
            provenance="manual-review",
            note="human transcription of %s" % name,
        )

    gold_captures = {}
    for name in args.gold_pages:
        capture = capture_page(store, client, name, baseline.page_family, "archive")
        store.set_gold(
            capture.capture_id,
            source["gold"][name],
            args.actor,
            args.verified_at,
            provenance="manual-review",
            note="human transcription of %s" % name,
        )
        gold_captures[name] = capture
    live_captures = []
    for ordinal, name in enumerate(args.live_pages, start=1):
        live_captures.append(capture_page(store, client, name, baseline.page_family, "live", ordinal))

    window = store.create_window(
        candidate.digest,
        config.digest,
        min(1, len(live_captures)),
        len(live_captures),
        args.actor,
        args.note,
        EXTRACTOR_VERSION,
        gold_capture_ids=[capture.capture_id for capture in gold_captures.values()],
    )
    print(
        json.dumps(
            {
                "window_id": window["window_id"],
                "candidate": candidate.label,
                "candidate_digest": candidate.digest,
                "baseline_digest": baseline.digest,
                "pages": len(window["pages"]),
                "jobs": len(window["jobs"]),
                "database": args.database,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    store.close()
    return 0


def cmd_review(args) -> int:
    store = open_store(args.database)
    window = store.get_window(args.window)
    status = window["status"]
    completed = 0
    if status == "open":
        completed = run_shadow(store, args.window, args.worker_id)
    if status in ("activated", "stale"):
        stored = window["evaluation"]
        if stored is None:
            raise SystemExit("window is %s and has no stored decision" % status)
        report = stored["report"]
    else:
        report = evaluate_window(store, args.window)
    summary = {
        "window_id": args.window,
        "shadow_jobs_completed": completed,
        "decision": report["decision"],
        "decision_reasons": report["decision_reasons"],
        "families": {
            family: {
                "passed": data["passed"],
                "metrics": data["metrics"],
                "reasons": data["reasons"],
            }
            for family, data in report["families"].items()
        },
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.pages:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    store.close()
    return 0


def cmd_activate(args) -> int:
    store = open_store(args.database)
    result = store.activate_window(args.window, args.actor, args.note)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    store.close()
    return 0


def cmd_production(args) -> int:
    source = load("source.json")
    store = open_store(args.database)
    runner = ProductionRunner(store, circuit_failure_threshold=store.circuit_threshold(args.family))
    client = SupplierClient(args.base_url)
    outcomes = []
    for name in args.pages:
        body, headers, url = client.fetch(name)
        outcomes.append(runner.process(url, args.family, body, headers, source_tag="live"))
    print(json.dumps(outcomes, ensure_ascii=False, indent=2, default=str))
    store.close()
    return 0


def cmd_show(args) -> int:
    store = open_store(args.database)
    if args.window:
        print(json.dumps(store.get_window(args.window), ensure_ascii=False, indent=2))
    else:
        print(json.dumps({
            "windows": store.list_windows(),
            "active": {
                family: active.to_json()
                for family in ("resource-detail", "series")
                if (active := store.active_rule(family)) is not None
            },
            "activation_events": store.activation_events(),
            "circuit_events": store.circuit_events(),
            "rejected_results": store.rejected_results(),
        }, ensure_ascii=False, indent=2))
    store.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="目录抽取规则影子验证与熔断演练")
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed", help="register rules and freeze a review window")
    seed.add_argument("--database", default="rollout.sqlite3")
    seed.add_argument("--base-url", default="http://127.0.0.1:8765")
    seed.add_argument("--actor", default="ops")
    seed.add_argument("--verified-at", default="2026-11-01T00:00:00Z")
    seed.add_argument("--note", default="drill window")
    seed.add_argument("--candidate-label", default="resource-detail/2026.11")
    seed.add_argument("--gold-pages", nargs="+", default=["classic", "drift"])
    seed.add_argument("--live-pages", nargs="+", default=["live-a", "live-b"])
    seed.set_defaults(func=cmd_seed)

    review = sub.add_parser("review", help="run shadow workers and evaluate")
    review.add_argument("--database", default="rollout.sqlite3")
    review.add_argument("--window", required=True)
    review.add_argument("--worker-id", default="worker-1")
    review.add_argument("--pages", action="store_true", help="print the page-level report")
    review.set_defaults(func=cmd_review)

    activate = sub.add_parser("activate", help="activate a passing window")
    activate.add_argument("--database", default="rollout.sqlite3")
    activate.add_argument("--window", required=True)
    activate.add_argument("--actor", default="ops")
    activate.add_argument("--note", default="window passed")
    activate.set_defaults(func=cmd_activate)

    production = sub.add_parser("production", help="run the active rule over pages")
    production.add_argument("--database", default="rollout.sqlite3")
    production.add_argument("--base-url", default="http://127.0.0.1:8765")
    production.add_argument("--family", default="resource-detail")
    production.add_argument("--pages", nargs="+", required=True)
    production.set_defaults(func=cmd_production)

    show = sub.add_parser("show", help="print stored audit data")
    show.add_argument("--database", default="rollout.sqlite3")
    show.add_argument("--window")
    show.set_defaults(func=cmd_show)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
