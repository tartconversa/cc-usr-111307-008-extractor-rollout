"""Deterministic, side-effect-free extraction with node-level evidence.

The extractor is deliberately strict.  A page either yields every required
field with no ambiguity and no semantic violation, or the whole page is
quarantined.  Callers must never turn a quarantine into empty strings or
"take the first match": that is exactly how a recommendation card silently
became the resource owner in the incident this module guards against.

Every field result carries evidence: the selector used, the matched node
paths, line/column, attributes and a text excerpt.  Evidence refers to the
captured response bytes and rule digest, so a reviewer can reproduce a
decision from storage alone.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

from .html_subset import Document, Element, is_within
from .rules import FieldRule, ReleaseCandidate, RulePackage, SemanticConstraint

EXTRACTOR_VERSION = "extractor/1.0"

# Outcomes.  ``OK`` is the only pass.
FIELD_OK = "ok"
FIELD_MISSING = "missing"
FIELD_AMBIGUOUS = "ambiguous"
FIELD_CONSTRAINT_FAILED = "constraint_failed"

# Quarantine reasons for a whole page.
PAGE_OK = "ok"
PAGE_MISSING_REQUIRED = "missing_required"
PAGE_AMBIGUOUS = "ambiguous"
PAGE_SEMANTIC_VIOLATION = "semantic_violation"
PAGE_SCOPE_MISSING = "scope_missing"
PAGE_SCOPE_AMBIGUOUS = "scope_ambiguous"


class ExtractionError(RuntimeError):
    """A rule could not be evaluated for reasons other than page content."""


def _constraint_failure(
    constraint: SemanticConstraint,
    value: str,
    within_scope: bool,
) -> str | None:
    if constraint.kind == "nonempty":
        return None if value.strip() else "value is blank"
    if constraint.kind == "within_resource":
        return None if within_scope else "node is outside the resource scope"
    if constraint.kind == "max_length":
        return None if len(value) <= constraint.value else "value exceeds max_length"
    if constraint.kind == "matches":
        return None if re.fullmatch(constraint.value, value) else "value does not match pattern"
    raise ExtractionError("unknown semantic constraint %r" % constraint.kind)


def _node_evidence(element: Element, text: str | None = None, within_scope: bool | None = None) -> dict[str, Any]:
    evidence = {
        "path": element.path(),
        "line": element.line,
        "column": element.column,
        "tag": element.tag,
        "attrs": dict(sorted(element.attrs.items())),
        "text": element.text()[:200] if text is None else text[:200],
    }
    if within_scope is not None:
        evidence["within_scope"] = within_scope
    return evidence


@dataclass
class FieldResult:
    name: str
    locator: str
    required: bool
    status: str
    value: str | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "locator": self.locator,
            "required": self.required,
            "status": self.status,
            "value": self.value,
            "candidates": self.candidates,
            "reasons": list(self.reasons),
        }


@dataclass
class PageExtraction:
    page_family: str
    rule_version: str
    rule_digest: str
    extractor_version: str
    url: str
    response_digest: str
    status: str
    fields: dict[str, FieldResult]
    resource_scope: dict[str, Any] | None
    reasons: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == PAGE_OK

    def values(self) -> dict[str, str]:
        if not self.ok:
            raise ExtractionError("page is quarantined; refusing to expose values")
        return {name: result.value for name, result in self.fields.items() if result.status == FIELD_OK}

    def to_json(self) -> dict[str, Any]:
        return {
            "page_family": self.page_family,
            "rule_version": self.rule_version,
            "rule_digest": self.rule_digest,
            "extractor_version": self.extractor_version,
            "url": self.url,
            "response_digest": self.response_digest,
            "status": self.status,
            "reasons": list(self.reasons),
            "resource_scope": self.resource_scope,
            "fields": {name: result.to_json() for name, result in self.fields.items()},
        }


class Extractor:
    """Runs one immutable rule package against one captured page."""

    version = EXTRACTOR_VERSION

    def __init__(self, rule: RulePackage) -> None:
        # Re-validate so a package cannot be mutated into an unsafe state via
        # duck typing; RulePackage fields are frozen but callers may pass
        # structurally-compatible objects.
        self.rule = RulePackage.from_json(rule.to_json())

    def extract(self, html: str | bytes, url: str, response_digest: str) -> PageExtraction:
        document = Document.parse(html)
        scope, scope_evidence, scope_reasons, scope_status = self._resolve_scope(document)
        reasons: list[str] = list(scope_reasons)
        fields: dict[str, FieldResult] = {}
        for field_rule in self.rule.fields:
            result = self._extract_field(document, field_rule, scope)
            fields[field_rule.name] = result
            reasons.extend("%s: %s" % (field_rule.name, reason) for reason in result.reasons)

        status = PAGE_OK
        if scope_status != PAGE_OK:
            status = scope_status
        elif any(result.status == FIELD_MISSING and result.required for result in fields.values()):
            status = PAGE_MISSING_REQUIRED
        elif any(result.status == FIELD_AMBIGUOUS for result in fields.values()):
            status = PAGE_AMBIGUOUS
        elif any(result.status == FIELD_CONSTRAINT_FAILED for result in fields.values()):
            status = PAGE_SEMANTIC_VIOLATION

        return PageExtraction(
            page_family=self.rule.page_family,
            rule_version=self.rule.version,
            rule_digest=self.rule.digest,
            extractor_version=self.version,
            url=url,
            response_digest=response_digest,
            status=status,
            fields=fields,
            resource_scope=scope_evidence,
            reasons=reasons,
        )

    def _resolve_scope(self, document: Document):
        if not self.rule.resource_scope:
            return None, None, [], PAGE_OK
        matched = document.select(self.rule.resource_scope)
        if not matched:
            return None, None, ["resource scope matched no node"], PAGE_SCOPE_MISSING
        if len(matched) > 1:
            return (
                None,
                {"locator": self.rule.resource_scope, "candidates": [_node_evidence(node) for node in matched]},
                ["resource scope matched %d nodes" % len(matched)],
                PAGE_SCOPE_AMBIGUOUS,
            )
        node = matched[0]
        return (
            [node],
            {"locator": self.rule.resource_scope, "candidates": [_node_evidence(node)]},
            [],
            PAGE_OK,
        )

    def _extract_field(self, document: Document, rule: FieldRule, scope) -> FieldResult:
        result = FieldResult(name=rule.name, locator=rule.locator, required=rule.required, status=FIELD_MISSING)
        matched = document.select(rule.locator)
        result.candidates = [
            _node_evidence(node, within_scope=is_within(node, scope) if scope else None)
            for node in matched
        ]
        if not matched:
            result.reasons.append("missing_required" if rule.required else "missing_optional")
            return result
        if len(matched) > 1:
            result.status = FIELD_AMBIGUOUS
            result.reasons.append("locator matched %d nodes" % len(matched))
            return result

        node = matched[0]
        value = node.text()
        within_scope = is_within(node, scope) if scope else False
        failures = [
            failure
            for constraint in rule.constraints
            if (failure := _constraint_failure(constraint, value, within_scope)) is not None
        ]
        result.value = value
        if failures:
            result.status = FIELD_CONSTRAINT_FAILED
            result.reasons.extend(failures)
            return result
        result.status = FIELD_OK
        return result


def extract_with_candidate(
    candidate: ReleaseCandidate,
    page_family: str,
    html: str | bytes,
    url: str,
    response_digest: str,
) -> PageExtraction:
    return Extractor(candidate.rule_for(page_family)).extract(html, url, response_digest)


__all__ = [
    "EXTRACTOR_VERSION",
    "ExtractionError",
    "Extractor",
    "FIELD_AMBIGUOUS",
    "FIELD_CONSTRAINT_FAILED",
    "FIELD_MISSING",
    "FIELD_OK",
    "PAGE_AMBIGUOUS",
    "PAGE_MISSING_REQUIRED",
    "PAGE_OK",
    "PAGE_SCOPE_AMBIGUOUS",
    "PAGE_SCOPE_MISSING",
    "PAGE_SEMANTIC_VIOLATION",
    "PageExtraction",
    "extract_with_candidate",
]
