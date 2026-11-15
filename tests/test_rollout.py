import json
import threading
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from src.extractor import (  # noqa: E402
    PAGE_AMBIGUOUS,
    PAGE_MISSING_REQUIRED,
    PAGE_OK,
    PAGE_SEMANTIC_VIOLATION,
    Extractor,
)
from src.review import DECISION_HOLD, DECISION_PASS, evaluate_window  # noqa: E402
from src.html_subset import LocatorSyntaxError  # noqa: E402
from src.rules import (  # noqa: E402
    FamilyThresholds,
    FieldRule,
    ReleaseCandidate,
    ReleaseConfig,
    RulePackage,
    RuleValidationError,
    SemanticConstraint,
)
from src.store import (  # noqa: E402
    GoldError,
    ImmutableRuleError,
    Store,
    StaleWorkerError,
    WindowError,
)
from src.workers import ProductionRunner, ShadowWorker, run_shadow  # noqa: E402
from src import source  # noqa: E402
from src.extractor import EXTRACTOR_VERSION  # noqa: E402

FIXTURES = ROOT / "fixtures"
SOURCE = json.loads((FIXTURES / "source.json").read_text(encoding="utf-8"))
RULES = json.loads((FIXTURES / "rules.json").read_text(encoding="utf-8"))


def baseline_package(**overrides):
    payload = json.loads(json.dumps(RULES["baseline"][0]))
    payload.update(overrides)
    return RulePackage.from_json(payload)


def candidate_package(**overrides):
    payload = json.loads(json.dumps(RULES["candidate"][0]))
    payload.update(overrides)
    return RulePackage.from_json(payload)


def family_thresholds(**overrides):
    values = {
        "family": "resource-detail",
        "min_pages": 4,
        "min_gold_pages": 2,
        "min_live_pages": 2,
    }
    values.update(overrides)
    return FamilyThresholds(**values)


class RolloutFixture(unittest.TestCase):
    """Shared store with an active baseline, a candidate and a frozen window."""

    def setUp(self):
        self.store = Store()
        self.baseline = baseline_package()
        self.candidate = candidate_package()
        self.store.register_rule(self.baseline)
        self.store.set_initial_active(
            "resource-detail", self.baseline.digest, "ops", "seed baseline", circuit_failure_threshold=2
        )
        self.release = ReleaseCandidate("resource-detail/2026.11", (self.candidate,))
        self.store.register_candidate(self.release)
        self.config = ReleaseConfig(thresholds=(family_thresholds(),), circuit_failure_threshold=2)
        self.store.register_config(self.config)
        self.gold_captures = []
        for name in ("classic", "drift"):
            capture = self.store.capture(
                "http://drill/%s" % name,
                "resource-detail",
                SOURCE["pages"][name].encode("utf-8"),
                {"content-type": "text/html"},
                source_tag="archive",
            )
            self.store.set_gold(
                capture.capture_id,
                SOURCE["gold"][name],
                "alice",
                "2026-11-01T00:00:00Z",
                provenance="manual-review",
                note="drill",
            )
            self.gold_captures.append(capture)
        self.live_captures = []
        for ordinal, name in enumerate(("live-a", "live-b"), start=1):
            self.live_captures.append(
                self.store.capture(
                    "http://drill/%s" % name,
                    "resource-detail",
                    SOURCE["pages"][name].encode("utf-8"),
                    {},
                    source_tag="live",
                    sample_ordinal=ordinal,
                )
            )

    def tearDown(self):
        self.store.close()

    def make_window(self, candidate=None, config=None, gold_ids=None, start=1, end=2):
        candidate = candidate or self.release
        config = config or self.config
        return self.store.create_window(
            candidate.digest,
            config.digest,
            start,
            end,
            "alice",
            "drill",
            EXTRACTOR_VERSION,
            gold_capture_ids=gold_ids,
        )

    def completed_report(self, window=None, candidate=None):
        window = window or self.make_window()
        run_shadow(self.store, window["window_id"])
        return evaluate_window(self.store, window["window_id"])


class IncidentReproductionTests(RolloutFixture):
    def test_baseline_nonempty_recommendation_is_quarantined(self):
        page = SOURCE["pages"]["drift"]
        extraction = Extractor(self.baseline).extract(page, "http://drill/drift", "x")
        self.assertEqual(extraction.status, PAGE_SEMANTIC_VIOLATION)
        provider = extraction.fields["provider"]
        self.assertEqual(provider.value, "推荐机构")  # non-empty, as in the incident
        self.assertEqual(provider.status, "constraint_failed")
        self.assertIn("outside the resource scope", " ".join(provider.reasons))
        self.assertFalse(provider.candidates[0]["within_scope"])
        with self.assertRaises(Exception):
            extraction.values()

    def test_candidate_recovers_owner_and_keeps_scope(self):
        extraction = Extractor(self.candidate).extract(
            SOURCE["pages"]["drift"], "http://drill/drift", "x"
        )
        self.assertEqual(extraction.status, PAGE_OK)
        self.assertEqual(extraction.values()["provider"], "甲供方")
        self.assertTrue(extraction.fields["provider"].candidates[0]["within_scope"])

    def test_page_level_report_explains_the_incident(self):
        report = self.completed_report()
        self.assertEqual(report["decision"], DECISION_PASS)
        family = report["families"]["resource-detail"]
        drift = next(page for page in family["pages"] if page["url"].endswith("/drift"))
        self.assertEqual(drift["split"], "gold")
        self.assertEqual(drift["baseline"]["status"], PAGE_SEMANTIC_VIOLATION)
        self.assertEqual(drift["candidate"]["status"], PAGE_OK)
        self.assertEqual(drift["candidate"]["fields"]["provider"]["value"], "甲供方")
        self.assertEqual(len(drift["baseline_gold_conflicts"]), 1)
        self.assertEqual(drift["gold_conflicts"], [])
        self.assertIn("provider", " ".join(drift["diff"]["reasons"]))
        live_pages = [page for page in family["pages"] if page["split"] == "live"]
        self.assertTrue(all(page["gold"] is None for page in live_pages))
        self.assertTrue(all(page["drift_hint"] for page in live_pages))


class ExtractorStrictnessTests(unittest.TestCase):
    def extract(self, html, package):
        return Extractor(package).extract(html, "http://drill/page", "x")

    def test_missing_required_field_quarantines_page(self):
        html = "<main id='resource'><h1>T</h1><div class='owner'>O</div></main>"
        package = candidate_package()
        package = RulePackage.from_json(
            {
                **package.to_json(),
                "fields": [
                    *package.to_json()["fields"],
                    {"name": "series", "locator": ".series", "required": True},
                ],
            }
        )
        extraction = self.extract(html, package)
        self.assertEqual(extraction.status, PAGE_MISSING_REQUIRED)
        self.assertIsNone(extraction.fields["series"].value)
        self.assertFalse(extraction.ok)

    def test_ambiguous_field_quarantines_whole_page(self):
        html = (
            "<main id='resource'><h1>T</h1>"
            "<div class='owner'>A</div><div class='owner'>B</div></main>"
        )
        extraction = self.extract(html, candidate_package())
        self.assertEqual(extraction.status, PAGE_AMBIGUOUS)
        self.assertEqual(len(extraction.fields["provider"].candidates), 2)
        self.assertIsNone(extraction.fields["provider"].value)

    def test_scope_missing_or_ambiguous(self):
        package = candidate_package()
        extraction = self.extract("<html><h1>T</h1></html>", package)
        self.assertEqual(extraction.status, "scope_missing")
        html = "<main id='resource'><h1>T</h1><div class='owner'>A</div></main><main id='resource'></main>"
        extraction = self.extract(html, package)
        self.assertEqual(extraction.status, "scope_ambiguous")

    def test_optional_field_missing_is_allowed(self):
        html = "<main id='resource'><h1>T</h1><div class='owner'>A</div></main>"
        extraction = self.extract(html, candidate_package())
        self.assertEqual(extraction.status, PAGE_OK)
        self.assertEqual(extraction.fields["license"].status, "missing")
        self.assertNotIn("license", extraction.values())

    def test_semantic_constraints(self):
        html = "<main id='resource'><h1>T</h1><div class='owner'>  </div></main>"
        extraction = self.extract(html, candidate_package())
        self.assertEqual(extraction.status, PAGE_SEMANTIC_VIOLATION)
        self.assertIn("blank", " ".join(extraction.fields["provider"].reasons))

        package = RulePackage.from_json(
            {
                **candidate_package().to_json(),
                "fields": [
                    {
                        "name": "name",
                        "locator": "main#resource h1",
                        "required": True,
                        "constraints": [{"kind": "matches", "value": "[0-9]+"}],
                    }
                ],
            }
        )
        extraction = self.extract("<main id='resource'><h1>目录</h1></main>", package)
        self.assertEqual(extraction.status, PAGE_SEMANTIC_VIOLATION)

    def test_evidence_points_at_captured_nodes(self):
        extraction = self.extract(SOURCE["pages"]["drift"], candidate_package())
        evidence = extraction.fields["provider"].candidates[0]
        self.assertIn("main#resource", evidence["path"])
        self.assertIn("attrs", evidence)
        self.assertGreater(evidence["column"], 0)
        self.assertEqual(extraction.rule_digest, self.extract(SOURCE["pages"]["drift"], candidate_package()).rule_digest)


class RuleImmutabilityTests(unittest.TestCase):
    def test_digest_tracks_every_rule_field(self):
        package = candidate_package()
        changed = candidate_package(version="resource-detail/2026.12")
        self.assertNotEqual(package.digest, changed.digest)
        field_changed = RulePackage.from_json(
            {**package.to_json(), "fields": [dict(package.to_json()["fields"][0], locator="main#resource h2"), *package.to_json()["fields"][1:]]}
        )
        self.assertNotEqual(package.digest, field_changed.digest)

    def test_store_rejects_repointed_version(self):
        store = Store()
        package = candidate_package()
        store.register_rule(package)
        same = store.register_rule(candidate_package())
        self.assertEqual(same, package.digest)
        with self.assertRaises(ImmutableRuleError):
            store.register_rule(candidate_package(description="edited in place"))
        with self.assertRaises(ImmutableRuleError):
            store.register_rule(candidate_package(version="resource-detail/2026.11", description="other"))
        store.close()

    def test_unsupported_selector_is_rejected_at_registration(self):
        payload = candidate_package().to_json()
        payload["fields"][1]["locator"] = "main#resource > .owner:first-child"
        with self.assertRaises(LocatorSyntaxError):
            RulePackage.from_json(payload)

    def test_within_resource_requires_scope(self):
        payload = candidate_package().to_json()
        payload["resource_scope"] = None
        with self.assertRaises(RuleValidationError):
            RulePackage.from_json(payload)

    def test_candidate_requires_one_package_per_family(self):
        with self.assertRaises(RuleValidationError):
            ReleaseCandidate("dup", (candidate_package(), candidate_package()))

    def test_constraint_validation(self):
        with self.assertRaises(RuleValidationError):
            SemanticConstraint("matches", "(")
        with self.assertRaises(RuleValidationError):
            SemanticConstraint("max_length", 0)
        with self.assertRaises(RuleValidationError):
            SemanticConstraint("nonsense")
        with self.assertRaises(RuleValidationError):
            FieldRule("bad name", ".x")


class CaptureAndGoldTests(RolloutFixture):
    def test_capture_preserves_raw_bytes(self):
        body = b"<main id='resource'>\xe4\xb8\xad</main>"
        capture = self.store.capture("http://drill/raw", "resource-detail", body, {"x": "1"}, source_tag="archive")
        stored = self.store.get_capture(capture.capture_id)
        self.assertEqual(stored.body, body)
        self.assertEqual(stored.response_digest, capture.response_digest)
        self.assertEqual(stored.url, "http://drill/raw")
        self.assertEqual(stored.headers, {"x": "1"})

    def test_live_pages_cannot_receive_gold(self):
        live = self.store.capture(
            "http://drill/live-x", "resource-detail", b"<html></html>", source_tag="live", sample_ordinal=9
        )
        with self.assertRaises(GoldError):
            self.store.set_gold(live.capture_id, {"name": "x"}, "alice", "2026-11-01T00:00:00Z")

    def test_gold_cannot_be_overwritten(self):
        capture = self.gold_captures[0]
        with self.assertRaises(GoldError):
            self.store.set_gold(capture.capture_id, {"name": "other"}, "alice", "2026-11-02T00:00:00Z")
        same = self.store.get_gold(capture.capture_id)
        self.assertEqual(same["fields"], SOURCE["gold"]["classic"])
        self.assertEqual(same["reviewer"], "alice")

    def test_gold_requires_reviewer_and_provenance(self):
        capture = self.store.capture("http://drill/a", "resource-detail", b"<html></html>", source_tag="archive")
        with self.assertRaises(GoldError):
            self.store.set_gold(capture.capture_id, {"name": "x"}, "", "2026-11-01T00:00:00Z")
        with self.assertRaises(GoldError):
            self.store.set_gold(
                capture.capture_id, {"name": "x"}, "alice", "2026-11-01T00:00:00Z", provenance="extraction"
            )

    def test_truth_is_not_derived_from_extraction(self):
        capture = self.gold_captures[0]
        self.assertEqual(
            self.store.get_gold(capture.capture_id)["fields"],
            SOURCE["gold"]["classic"],
        )
        # A candidate extraction never writes into gold.
        extraction = Extractor(self.candidate).extract(
            capture.body, capture.url, capture.response_digest
        )
        self.assertEqual(self.store.get_gold(capture.capture_id)["fields"], SOURCE["gold"]["classic"])
        self.assertEqual(extraction.values()["provider"], SOURCE["gold"]["classic"]["provider"])


class WindowTests(RolloutFixture):
    def test_window_binds_fixed_pages_and_ordinals(self):
        self.store.capture(
            "http://drill/live-c",
            "resource-detail",
            SOURCE["pages"]["live-c"].encode("utf-8"),
            {},
            source_tag="live",
            sample_ordinal=3,
        )
        window = self.make_window(start=1, end=2)
        ordinals = sorted(page["sample_ordinal"] for page in window["pages"] if page["split"] == "live")
        self.assertEqual(ordinals, [1, 2])
        live_ids = {page["capture_id"] for page in window["pages"] if page["split"] == "live"}
        self.assertNotIn(
            self.store.capture(
                "http://drill/live-c", "resource-detail", SOURCE["pages"]["live-c"].encode("utf-8"),
                {}, source_tag="live", sample_ordinal=3,
            ).capture_id,
            live_ids,
        )
        with self.assertRaises(Exception):
            self.store.capture(
                "http://drill/later",
                "resource-detail",
                SOURCE["pages"]["live-a"].encode("utf-8"),
                {},
                source_tag="live",
                sample_ordinal=1,
            )
        self.assertEqual(len(self.store.get_window(window["window_id"])["pages"]), len(window["pages"]))

    def test_missing_shadow_result_holds_the_window(self):
        window = self.make_window()
        first_job = window["jobs"][0]
        worker = ShadowWorker(self.store, window["window_id"], "worker-1")
        leased = self.store.lease_jobs(window["window_id"], "worker-1", EXTRACTOR_VERSION, limit=1)
        self.assertEqual(len(leased), 1)
        job = leased[0]
        extraction = Extractor(self.store.get_rule(job.rule_digest)).extract(
            job.capture.body, job.capture.url, job.capture.response_digest
        )
        payload = extraction.to_json()
        payload.update({"job_id": job.job_id, "capture_id": job.capture_id})
        self.store.complete_job(job.job_id, job.lease_token, job.fence, "worker-1", payload)
        report = evaluate_window(self.store, window["window_id"])
        self.assertEqual(report["decision"], DECISION_HOLD)
        self.assertTrue(any("no shadow result" in reason for reason in report["decision_reasons"]))

    def test_family_sample_floor_cannot_be_covered_by_another_family(self):
        other = RulePackage.from_json(
            {
                **candidate_package().to_json(),
                "version": "other/1",
                "page_family": "other",
                "resource_scope": "main#resource",
                "fields": [
                    {"name": "name", "locator": "main#resource h1", "required": True,
                     "constraints": [{"kind": "nonempty"}]}
                ],
            }
        )
        self.store.register_rule(other)
        self.store.set_initial_active("other", other.digest, "ops", "seed other", circuit_failure_threshold=2)
        combined = ReleaseCandidate("combined", (self.candidate, other))
        self.store.register_candidate(combined)
        config = ReleaseConfig(
            thresholds=(
                family_thresholds(min_pages=4, min_gold_pages=2, min_live_pages=2),
                FamilyThresholds(family="other", min_pages=3, min_gold_pages=1, min_live_pages=1),
            ),
            circuit_failure_threshold=2,
        )
        self.store.register_config(config)
        # Give "other" plenty of perfect pages and gold; resource-detail stays small.
        for index in range(3):
            capture = self.store.capture(
                "http://drill/other-%d" % index, "other", b"<main id='resource'><h1>O</h1></main>",
                source_tag="archive",
            )
            self.store.set_gold(capture.capture_id, {"name": "O"}, "alice", "2026-11-01T00:00:00Z")
        window = self.make_window(candidate=combined, config=config)
        run_shadow(self.store, window["window_id"])
        report = evaluate_window(self.store, window["window_id"])
        # resource-detail has 4 pages, but "other" has 3 with only gold, no live.
        other_report = report["families"]["other"]
        self.assertFalse(other_report["passed"])
        self.assertTrue(any("live page" in reason for reason in other_report["reasons"]))
        self.assertEqual(report["decision"], DECISION_HOLD)

    def test_live_pages_do_not_claim_candidate_is_more_correct(self):
        report = self.completed_report()
        for page in report["families"]["resource-detail"]["pages"]:
            if page["split"] == "live":
                self.assertIsNone(page["gold"])
                self.assertEqual(page["gold_conflicts"], [])
                self.assertTrue(page["drift_hint"])


class WorkerFencingTests(RolloutFixture):
    def test_worker_with_wrong_extractor_version_is_refused(self):
        window = self.make_window()
        with self.assertRaises(WindowError):
            self.store.lease_jobs(window["window_id"], "w", "extractor/0.9")

    def test_old_worker_result_after_activation_is_rejected(self):
        window = self.make_window()
        leased = self.store.lease_jobs(window["window_id"], "worker-old", EXTRACTOR_VERSION, limit=1)
        old_job = leased[0]
        # The old lease expires and a fresh worker takes over the same job.
        with self.store._transaction() as conn:
            conn.execute(
                "UPDATE shadow_jobs SET lease_expires = ? WHERE job_id = ?",
                (0.0, old_job.job_id),
            )
        run_shadow(self.store, window["window_id"], worker_id="worker-new")
        report = evaluate_window(self.store, window["window_id"])
        self.assertEqual(report["decision"], DECISION_PASS)
        self.store.activate_window(window["window_id"], "alice", "pass")
        package = self.store.get_rule(old_job.rule_digest)
        extraction = Extractor(package).extract(
            old_job.capture.body, old_job.capture.url, old_job.capture.response_digest
        )
        payload = extraction.to_json()
        payload.update({"job_id": old_job.job_id, "capture_id": old_job.capture_id})
        with self.assertRaises(StaleWorkerError):
            self.store.complete_job(
                old_job.job_id, old_job.lease_token, old_job.fence, "worker-old", payload
            )
        rejected = self.store.rejected_results(window["window_id"])
        self.assertTrue(
            any("already completed" in row["reason"] or "lease" in row["reason"] for row in rejected)
        )

    def test_forged_rule_digest_is_rejected(self):
        window = self.make_window()
        leased = self.store.lease_jobs(window["window_id"], "w", EXTRACTOR_VERSION, limit=1)
        job = leased[0]
        payload = {
            "capture_id": job.capture_id,
            "page_family": job.page_family,
            "rule_digest": "0" * 64,
            "extractor_version": EXTRACTOR_VERSION,
            "response_digest": job.capture.response_digest,
            "status": "ok",
            "fields": {},
        }
        with self.assertRaises(StaleWorkerError):
            self.store.complete_job(job.job_id, job.lease_token, job.fence, "w", payload)
        self.assertTrue(self.store.rejected_results(window["window_id"]))

    def test_wrong_fence_is_rejected(self):
        window = self.make_window()
        leased = self.store.lease_jobs(window["window_id"], "w", EXTRACTOR_VERSION, limit=1)
        job = leased[0]
        payload = {
            "capture_id": job.capture_id,
            "page_family": job.page_family,
            "rule_digest": job.rule_digest,
            "extractor_version": EXTRACTOR_VERSION,
            "response_digest": job.capture.response_digest,
        }
        with self.assertRaises(StaleWorkerError):
            self.store.complete_job(job.job_id, job.lease_token, job.fence + 1, "w", payload)

    def test_relabelled_old_worker_payload_is_rejected(self):
        window = self.make_window()
        leased = self.store.lease_jobs(window["window_id"], "w", EXTRACTOR_VERSION, limit=1)
        job = leased[0]
        payload = {
            "capture_id": job.capture_id,
            "page_family": job.page_family,
            "rule_digest": "f" * 64,  # old worker labels its output as a newer version
            "extractor_version": EXTRACTOR_VERSION,
            "response_digest": job.capture.response_digest,
        }
        with self.assertRaises(StaleWorkerError):
            self.store.complete_job(job.job_id, job.lease_token, job.fence, "w", payload)


class ActivationTests(RolloutFixture):
    def test_hold_decision_does_not_activate(self):
        # A candidate whose provider locator cannot see the owner field is
        # plausible-looking but fails independent gold on the drift page.
        broken_rule = RulePackage.from_json(
            {
                **candidate_package().to_json(),
                "version": "resource-detail/broken",
                "fields": [
                    field
                    if field["name"] != "provider"
                    else {**field, "locator": "main#resource .provider"}
                    for field in candidate_package().to_json()["fields"]
                ],
            }
        )
        self.store.register_rule(broken_rule)
        broken_release = ReleaseCandidate("resource-detail/broken", (broken_rule,))
        self.store.register_candidate(broken_release)
        job_window = self.make_window(candidate=broken_release)
        run_shadow(self.store, job_window["window_id"])
        report = evaluate_window(self.store, job_window["window_id"])
        self.assertEqual(report["decision"], DECISION_HOLD)
        self.assertTrue(any("gold conflict" in reason for reason in report["decision_reasons"]))
        with self.assertRaises(WindowError):
            self.store.activate_window(report["window_id"], "alice", "try")
        self.assertEqual(self.store.active_rule("resource-detail").rule_digest, self.baseline.digest)

    def test_active_change_during_review_marks_window_stale(self):
        window = self.make_window()
        run_shadow(self.store, window["window_id"])
        evaluate_window(self.store, window["window_id"])
        # Simulate another release changing the active rule after evaluation.
        other = RulePackage.from_json(
            {**baseline_package().to_json(), "version": "resource-detail/hotfix", "description": "hotfix"}
        )
        self.store.register_rule(other)
        with self.store._transaction() as conn:
            conn.execute(
                "UPDATE active_rules SET rule_digest = ?, epoch = epoch + 1 WHERE page_family = 'resource-detail'",
                (other.digest,),
            )
        with self.assertRaises(WindowError):
            self.store.activate_window(window["window_id"], "alice", "try")
        self.assertEqual(self.store.get_window(window["window_id"])["status"], "stale")
        self.assertEqual(self.store.active_rule("resource-detail").rule_digest, other.digest)

    def test_activation_moves_epoch_and_records_events(self):
        report = self.completed_report()
        self.assertEqual(report["decision"], DECISION_PASS)
        result = self.store.activate_window(report["window_id"], "alice", "pass")
        active = self.store.active_rule("resource-detail")
        self.assertEqual(active.rule_digest, self.candidate.digest)
        self.assertEqual(active.epoch, 2)
        events = self.store.activation_events("resource-detail")
        self.assertEqual(events[-1]["from_digest"], self.baseline.digest)
        self.assertEqual(events[-1]["to_digest"], self.candidate.digest)
        self.assertEqual(events[-1]["window_id"], report["window_id"])
        with self.assertRaises(WindowError):
            self.store.activate_window(report["window_id"], "alice", "again")


class BreakerTests(RolloutFixture):
    def activate_candidate(self):
        report = self.completed_report()
        self.assertEqual(report["decision"], DECISION_PASS)
        self.store.activate_window(report["window_id"], "alice", "pass")
        return report

    def test_consecutive_semantic_failures_open_family_and_retain_page(self):
        self.activate_candidate()
        runner = ProductionRunner(self.store, circuit_failure_threshold=self.store.circuit_threshold("resource-detail"))
        broken = SOURCE["pages"]["series-broken"].encode("utf-8")
        # Give the new layout two identical owner nodes to trigger ambiguity.
        ambiguous = (
            b"<html><main id='resource'><h1>T</h1>"
            b"<div class='owner'>A</div><div class='owner'>B</div></main></html>"
        )
        first = runner.process("http://drill/new1", "resource-detail", ambiguous, source_tag="live")
        self.assertEqual(first["circuit"], "closed")
        self.assertIsNone(first["emitted"])
        self.assertIsNotNone(first["quarantined"])
        second = runner.process("http://drill/new2", "resource-detail", ambiguous, source_tag="live")
        self.assertEqual(second["circuit"], "open")
        active = self.store.active_rule("resource-detail")
        self.assertTrue(active.circuit_open)
        self.assertEqual(active.rule_digest, self.candidate.digest)  # no auto-fallback
        third = runner.process(
            "http://drill/new3", "resource-detail",
            SOURCE["pages"]["classic"].encode("utf-8"), source_tag="live",
        )
        self.assertIsNone(third["emitted"])
        self.assertEqual(third["reason"], "circuit open; page retained for a reviewed rule revision")
        events = self.store.circuit_events("resource-detail")
        self.assertEqual([event["action"] for event in events], ["open"])

    def test_breaker_never_falls_back_to_known_bad_rule(self):
        self.activate_candidate()
        runner = ProductionRunner(self.store, circuit_failure_threshold=1)
        broken = b"<html><main id='resource'><h1>T</h1><div class='owner'>A</div><div class='owner'>B</div></main></html>"
        runner.process("http://drill/new1", "resource-detail", broken, source_tag="live")
        self.assertTrue(self.store.active_rule("resource-detail").circuit_open)
        self.assertNotEqual(self.store.active_rule("resource-detail").rule_digest, self.baseline.digest)
        # Pages are retained as captures for a future revision.
        captures = [row for row in self.store.list_captures("resource-detail") if row["url"].endswith("new1")]
        self.assertTrue(captures)

    def test_review_of_the_same_broken_rule_is_refused(self):
        self.activate_candidate()
        runner = ProductionRunner(self.store, circuit_failure_threshold=1)
        broken = b"<html><main id='resource'><h1>T</h1><div class='owner'>A</div><div class='owner'>B</div></main></html>"
        runner.process("http://drill/new1", "resource-detail", broken, source_tag="live")
        active = self.store.active_rule("resource-detail")
        self.assertTrue(active.circuit_open)
        # Re-running the same candidate through a window would "pass" its gold
        # pages, but must not silently re-arm the known broken rule.
        window = self.make_window()
        run_shadow(self.store, window["window_id"])
        report = evaluate_window(self.store, window["window_id"])
        if report["decision"] == DECISION_PASS:
            with self.assertRaises(WindowError):
                self.store.activate_window(window["window_id"], "alice", "re-arm")
        self.assertTrue(self.store.active_rule("resource-detail").circuit_open)


class NewFamilyBreakerTests(unittest.TestCase):
    """A brand-new family that keeps failing semantically is isolated."""

    def setUp(self):
        self.store = Store()
        payload = json.loads(json.dumps(RULES["series_baseline"][0]))
        self.series = RulePackage.from_json(payload)
        self.store.register_rule(self.series)
        self.store.set_initial_active("series", self.series.digest, "ops", "new family", circuit_failure_threshold=2)
        self.runner = ProductionRunner(self.store, circuit_failure_threshold=self.store.circuit_threshold("series"))

    def tearDown(self):
        self.store.close()

    def test_breaker_opens_and_keeps_original_pages(self):
        for name in ("series-live-1", "series-live-2"):
            outcome = self.runner.process(
                "http://drill/%s" % name, "series", SOURCE["pages"][name].encode("utf-8"), source_tag="live"
            )
            self.assertIsNotNone(outcome["emitted"])
            self.assertEqual(outcome["circuit"], "closed")
        first = self.runner.process(
            "http://drill/series-broken", "series", SOURCE["pages"]["series-broken"].encode("utf-8"),
            source_tag="live",
        )
        self.assertIsNone(first["emitted"])
        self.assertEqual(first["circuit"], "closed")
        second = self.runner.process(
            "http://drill/series-broken-2", "series", SOURCE["pages"]["series-broken"].encode("utf-8"),
            source_tag="live",
        )
        self.assertEqual(second["circuit"], "open")
        active = self.store.active_rule("series")
        self.assertTrue(active.circuit_open)
        self.assertEqual(active.rule_digest, self.series.digest)
        # Every page is retained as a capture and every rejection has a reason.
        retained = self.store.list_captures("series")
        self.assertEqual(len(retained), 4)
        outcomes = [row for row in self.store.page_outcomes("series") if not row["produced"]]
        self.assertTrue(outcomes)
        self.assertTrue(all("matched 2 nodes" in row["reason"] for row in outcomes))
        # Subsequent pages are retained but not emitted, and the circuit stays
        # open until a revised rule is reviewed.
        third = self.runner.process(
            "http://drill/series-live-3", "series",
            SOURCE["pages"]["series-live-1"].encode("utf-8"), source_tag="live",
        )
        self.assertIsNone(third["emitted"])
        self.assertEqual(third["circuit"], "open")
        self.assertTrue(self.store.active_rule("series").circuit_open)


class ExplicitGoldSelectionTests(RolloutFixture):
    def test_window_can_bind_a_named_subset_of_gold_pages(self):
        window = self.make_window(gold_ids=[self.gold_captures[0].capture_id])
        gold_pages = [page for page in window["pages"] if page["split"] == "gold"]
        self.assertEqual([page["capture_id"] for page in gold_pages], [self.gold_captures[0].capture_id])

    def test_window_rejects_unverified_gold_id(self):
        capture = self.store.capture(
            "http://drill/unverified", "resource-detail", b"<html></html>", source_tag="archive"
        )
        with self.assertRaises(WindowError):
            self.make_window(gold_ids=[capture.capture_id])


class PersistenceTests(RolloutFixture):
    def setUp(self):
        super().setUp()
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "rollout.sqlite3"

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_bytes_and_report_survive_store_reopen(self):
        file_store = Store(self.path)
        capture = file_store.capture(
            "http://drill/persist", "resource-detail", b"<main id='resource'><h1>T</h1></main>",
            {"content-type": "text/html"}, source_tag="archive",
        )
        file_store.close()
        reopened = Store(self.path)
        try:
            restored = reopened.get_capture(capture.capture_id)
            self.assertEqual(restored.body, b"<main id='resource'><h1>T</h1></main>")
            self.assertEqual(restored.headers, {"content-type": "text/html"})
            self.assertEqual(restored.response_digest, capture.response_digest)
        finally:
            reopened.close()

    def test_activation_is_compare_and_swap_across_connections(self):
        file_store = Store(self.path)
        for package in (self.baseline, self.candidate):
            file_store.register_rule(package)
        file_store.register_candidate(self.release)
        file_store.register_config(self.config)
        file_store.set_initial_active("resource-detail", self.baseline.digest, "ops", "seed")
        for capture in [*self.gold_captures, *self.live_captures]:
            stored = file_store.capture(
                capture.url, capture.page_family, capture.body, capture.headers,
                source_tag=capture.source_tag, sample_ordinal=capture.sample_ordinal,
            )
            if capture.source_tag == "archive":
                file_store.set_gold(stored.capture_id, SOURCE["gold"]["classic" if "classic" in capture.url else "drift"], "alice", "2026-11-01T00:00:00Z")
        window = file_store.create_window(
            self.release.digest, self.config.digest, 1, 2, "alice", "drill", EXTRACTOR_VERSION
        )
        run_shadow(file_store, window["window_id"])
        report = evaluate_window(file_store, window["window_id"])
        self.assertEqual(report["decision"], DECISION_PASS)
        second = Store(self.path)
        try:
            file_store.activate_window(window["window_id"], "alice", "first")
            with self.assertRaises(WindowError):
                second.activate_window(window["window_id"], "bob", "second")
            self.assertEqual(second.active_rule("resource-detail").rule_digest, self.candidate.digest)
        finally:
            second.close()
            file_store.close()


class ReportAuditTests(RolloutFixture):
    def test_report_links_each_decision_to_pages_and_evidence(self):
        report = self.completed_report()
        family = report["families"]["resource-detail"]
        self.assertEqual(family["metrics"]["pages_total"], 4)
        for page in family["pages"]:
            self.assertTrue(page["capture_id"])
            self.assertTrue(page["response_digest"])
            self.assertIn("fields", page["candidate"])
            for field in page["candidate"]["fields"].values():
                for candidate in field["candidates"]:
                    self.assertIn("path", candidate)
                    self.assertIn("line", candidate)
        stored = self.store.get_window(report["window_id"])["evaluation"]
        self.assertEqual(stored["decision"], DECISION_PASS)
        self.assertEqual(stored["report"]["families"]["resource-detail"]["metrics"]["pages_total"], 4)

    def test_window_pins_extractor_and_rule_digests(self):
        window = self.make_window()
        self.assertEqual(window["extractor_version"], EXTRACTOR_VERSION)
        for job in window["jobs"]:
            self.assertEqual(job["extractor_version"], EXTRACTOR_VERSION)
            self.assertEqual(job["rule_digest"], self.candidate.digest)
            self.assertEqual(job["baseline_rule_digest"], self.baseline.digest)


class SourceDrillServiceTests(unittest.TestCase):
    def test_service_serves_new_layout_and_keeps_state(self):
        server = source.make_server()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = "http://127.0.0.1:%d" % server.server_port
            with urllib.request.urlopen(base + "/drift", timeout=5) as response:
                page = response.read().decode("utf-8")
            self.assertLess(page.index("推荐机构"), page.index("<main"))
            with urllib.request.urlopen(base + "/__state", timeout=5) as response:
                state = json.load(response)
            self.assertEqual(state["generation"], 1)
            self.assertTrue(state["requests"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
