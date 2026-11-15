"""Shadow workers and the production extraction runner.

Shadow workers only ever produce *differences*.  They never write production
output and they are pinned to the window's extractor version and rule digest.
The store fences results by lease token and epoch, so a worker that wakes up
after a window was invalidated cannot have its late output recorded as the
new version.

The production runner reads the active rule at call time, verifies the
digest immediately before recording, and feeds the family breaker on every
quarantined page.  It never composes output from two rule versions: the
entire page is extracted by one immutable package.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .extractor import EXTRACTOR_VERSION, Extractor
from .store import Store, WindowError


class WorkerError(RuntimeError):
    """A worker tried to operate outside its pinned window/version."""


@dataclass
class ShadowWorker:
    """Consumes one window's frozen jobs for one extractor version."""

    store: Store
    window_id: str
    worker_id: str
    extractor_version: str = EXTRACTOR_VERSION

    def run_once(self, limit: int = 10) -> int:
        jobs = self.store.lease_jobs(
            self.window_id,
            self.worker_id,
            self.extractor_version,
            limit=limit,
        )
        completed = 0
        for job in jobs:
            package = self.store.get_rule(job.rule_digest)
            if package.digest != job.rule_digest:
                raise WorkerError("store returned a rule whose digest does not match the job")
            if job.capture is None:
                raise WorkerError("leased job has no capture")
            extraction = Extractor(package).extract(
                job.capture.body,
                job.capture.url,
                job.capture.response_digest,
            )
            payload = extraction.to_json()
            payload["job_id"] = job.job_id
            payload["capture_id"] = job.capture_id
            self.store.complete_job(
                job.job_id,
                job.lease_token,
                job.fence,
                self.worker_id,
                payload,
            )
            completed += 1
        return completed


@dataclass
class ProductionRunner:
    """Runs one active extraction per page and records breaker outcomes.

    A page is emitted only when the active version extracted it cleanly.  A
    quarantined page emits nothing; it increments the family's consecutive
    failure counter and may open the family circuit.  The runner never falls
    back to an older rule: falling back to a version known to mis-extract is
    exactly the behaviour the breaker is meant to prevent.
    """

    store: Store
    circuit_failure_threshold: int = 3

    def process(
        self,
        url: str,
        page_family: str,
        body: bytes,
        headers: dict[str, str] | None = None,
        source_tag: str = "live",
        sample_ordinal: int | None = None,
    ) -> dict[str, Any]:
        capture = self.store.capture(
            url,
            page_family,
            body,
            headers,
            source_tag=source_tag,
            sample_ordinal=sample_ordinal,
        )
        active = self.store.active_rule(page_family)
        if active is None:
            raise WindowError("family %s has no active rule" % page_family)
        if active.circuit_open:
            self.store.record_production_outcome(
                capture.capture_id,
                page_family,
                active.rule_digest,
                "circuit_open",
                False,
                "circuit open for family %s" % page_family,
                failure=False,
                circuit_failure_threshold=self.circuit_failure_threshold,
            )
            return {
                "capture": capture.to_json(),
                "emitted": None,
                "active": active.to_json(),
                "quarantined": None,
                "circuit": "open",
                "reason": "circuit open; page retained for a reviewed rule revision",
            }
        package = self.store.get_rule(active.rule_digest)
        extraction = Extractor(package).extract(body, url, capture.response_digest)
        if extraction.ok:
            payload = extraction.to_json()
            self.store.record_production_result(
                capture.capture_id,
                page_family,
                active.rule_digest,
                extraction.status,
                payload,
            )
            updated = self.store.record_production_outcome(
                capture.capture_id,
                page_family,
                active.rule_digest,
                extraction.status,
                True,
                "ok",
                failure=False,
                circuit_failure_threshold=self.circuit_failure_threshold,
            )
            return {
                "capture": capture.to_json(),
                "emitted": payload,
                "active": updated.to_json(),
                "quarantined": None,
                "circuit": updated.circuit_state,
                "reason": None,
            }
        payload = extraction.to_json()
        updated = self.store.record_production_outcome(
            capture.capture_id,
            page_family,
            active.rule_digest,
            extraction.status,
            False,
            "; ".join(extraction.reasons),
            failure=True,
            circuit_failure_threshold=self.circuit_failure_threshold,
        )
        return {
            "capture": capture.to_json(),
            "emitted": None,
            "active": updated.to_json(),
            "quarantined": payload,
            "circuit": updated.circuit_state,
            "reason": "; ".join(extraction.reasons),
        }



def run_shadow(
    store: Store,
    window_id: str,
    worker_id: str = "worker-1",
    extractor_version: str = EXTRACTOR_VERSION,
) -> int:
    """Convenience: drain a window's jobs with one local worker."""

    worker = ShadowWorker(store, window_id, worker_id, extractor_version)
    total = 0
    while True:
        count = worker.run_once()
        total += count
        if count == 0:
            return total


__all__ = [
    "EXTRACTOR_VERSION",
    "ProductionRunner",
    "ShadowWorker",
    "WorkerError",
    "run_shadow",
]
