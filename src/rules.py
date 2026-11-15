"""Immutable extraction-rule packages, release candidates and thresholds.

A :class:`RulePackage` is content addressed.  Its ``digest`` covers every
rule field, the target page family, the resource scope and the semantic
constraints.  The store refuses to register two different payloads under the
same human version label, so a version can never be re-pointed after the
fact.

A :class:`ReleaseCandidate` groups one package per page family.  Packaging
several families into one candidate lets a release window measure every
family separately while activation still replaces each family's active rule
atomically.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Any, Iterable

from .html_subset import parse_selector

RULE_SCHEMA = "catalog.extract-rule/v1"
CANDIDATE_SCHEMA = "catalog.release-candidate/v1"
CONFIG_SCHEMA = "catalog.release-config/v1"
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_FAMILY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class RuleValidationError(ValueError):
    """A rule, candidate or release configuration is malformed."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def is_digest(value: str) -> bool:
    return bool(_DIGEST_RE.fullmatch(value or ""))


@dataclass(frozen=True)
class SemanticConstraint:
    """A value or scope constraint attached to a field.

    Supported kinds:

    ``nonempty``
        The extracted text must not be blank.
    ``within_resource``
        The selected node must live inside the package's resource scope.
        This is what stops a recommendation card from masquerading as the
        resource owner.
    ``max_length``
        ``value`` is the maximum number of characters.
    ``matches``
        ``value`` is a regular expression; the whole value must match.
    """

    kind: str
    value: Any = None

    def __post_init__(self) -> None:
        if self.kind in {"nonempty", "within_resource"}:
            if self.value is not None:
                raise RuleValidationError("%s does not take a value" % self.kind)
        elif self.kind == "max_length":
            if not isinstance(self.value, int) or isinstance(self.value, bool) or self.value <= 0:
                raise RuleValidationError("max_length needs a positive integer")
        elif self.kind == "matches":
            if not isinstance(self.value, str) or not self.value:
                raise RuleValidationError("matches needs a regular expression")
            try:
                re.compile(self.value)
            except re.error as error:
                raise RuleValidationError("invalid regular expression: %s" % error) from error
        else:
            raise RuleValidationError("unknown semantic constraint %r" % self.kind)

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"kind": self.kind}
        if self.value is not None:
            payload["value"] = self.value
        return payload

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> "SemanticConstraint":
        if not isinstance(value, dict):
            raise RuleValidationError("semantic constraint must be an object")
        return cls(value.get("kind", ""), value.get("value"))


@dataclass(frozen=True)
class FieldRule:
    name: str
    locator: str
    required: bool = True
    constraints: tuple[SemanticConstraint, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not _NAME_RE.fullmatch(self.name or ""):
            raise RuleValidationError("invalid field name %r" % (self.name,))
        if not isinstance(self.locator, str) or not self.locator.strip():
            raise RuleValidationError("field %s has an empty locator" % self.name)
        if not isinstance(self.required, bool):
            raise RuleValidationError("field %s required must be boolean" % self.name)
        parse_selector(self.locator)  # Reject syntax outside the documented subset.
        kinds = [constraint.kind for constraint in self.constraints]
        if len(kinds) != len(set(kinds)):
            raise RuleValidationError("field %s repeats a semantic constraint" % self.name)

    def constraint(self, kind: str) -> SemanticConstraint | None:
        return next((item for item in self.constraints if item.kind == kind), None)

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "locator": self.locator,
            "required": self.required,
            "constraints": [item.to_json() for item in self.constraints],
        }

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> "FieldRule":
        if not isinstance(value, dict):
            raise RuleValidationError("field rule must be an object")
        raw_constraints = value.get("constraints", [])
        if not isinstance(raw_constraints, list):
            raise RuleValidationError("field constraints must be a list")
        return cls(
            name=value.get("name", ""),
            locator=value.get("locator", ""),
            required=value.get("required", True),
            constraints=tuple(SemanticConstraint.from_json(item) for item in raw_constraints),
        )


@dataclass(frozen=True)
class RulePackage:
    version: str
    page_family: str
    fields: tuple[FieldRule, ...]
    resource_scope: str | None = None
    description: str = ""
    schema: str = RULE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != RULE_SCHEMA:
            raise RuleValidationError("unsupported rule schema %r" % (self.schema,))
        if not isinstance(self.version, str) or not self.version.strip():
            raise RuleValidationError("rule version is required")
        if not _FAMILY_RE.fullmatch(self.page_family or ""):
            raise RuleValidationError("invalid page family %r" % (self.page_family,))
        if not self.fields:
            raise RuleValidationError("rule must declare at least one field")
        names = [item.name for item in self.fields]
        if len(names) != len(set(names)):
            raise RuleValidationError("duplicate field names")
        if self.resource_scope is not None:
            if not isinstance(self.resource_scope, str) or not self.resource_scope.strip():
                raise RuleValidationError("resource scope must be a non-empty selector")
            parse_selector(self.resource_scope)
        for item in self.fields:
            if item.constraint("within_resource") and not self.resource_scope:
                raise RuleValidationError(
                    "field %s uses within_resource but the package has no resource scope" % item.name
                )
        if not isinstance(self.description, str):
            raise RuleValidationError("description must be a string")

    def required_fields(self) -> tuple[FieldRule, ...]:
        return tuple(item for item in self.fields if item.required)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "page_family": self.page_family,
            "resource_scope": self.resource_scope,
            "description": self.description,
            "fields": [item.to_json() for item in self.fields],
        }

    @property
    def digest(self) -> str:
        return sha256_json(self.to_json())

    @property
    def short_digest(self) -> str:
        return self.digest[:12]

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> "RulePackage":
        if not isinstance(value, dict):
            raise RuleValidationError("rule package must be an object")
        raw_fields = value.get("fields", [])
        if not isinstance(raw_fields, list):
            raise RuleValidationError("rule fields must be a list")
        return cls(
            version=value.get("version", ""),
            page_family=value.get("page_family", ""),
            fields=tuple(FieldRule.from_json(item) for item in raw_fields),
            resource_scope=value.get("resource_scope"),
            description=value.get("description", ""),
            schema=value.get("schema", RULE_SCHEMA),
        )


@dataclass(frozen=True)
class ReleaseCandidate:
    label: str
    rules: tuple[RulePackage, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.label, str) or not self.label.strip():
            raise RuleValidationError("candidate label is required")
        if not self.rules:
            raise RuleValidationError("candidate must contain at least one rule package")
        families = [rule.page_family for rule in self.rules]
        if len(families) != len(set(families)):
            raise RuleValidationError("candidate contains two packages for the same family")

    @property
    def families(self) -> tuple[str, ...]:
        return tuple(sorted(rule.page_family for rule in self.rules))

    def rule_for(self, family: str) -> RulePackage:
        for rule in self.rules:
            if rule.page_family == family:
                return rule
        raise KeyError(family)

    @property
    def rule_digests(self) -> dict[str, str]:
        return {rule.page_family: rule.digest for rule in self.rules}

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": CANDIDATE_SCHEMA,
            "label": self.label,
            "rules": [
                {"page_family": rule.page_family, "version": rule.version, "rule_digest": rule.digest}
                for rule in sorted(self.rules, key=lambda item: item.page_family)
            ],
        }

    @property
    def digest(self) -> str:
        return sha256_json(self.to_json())

    @classmethod
    def from_json(cls, value: dict[str, Any], rules: Iterable[RulePackage] = ()) -> "ReleaseCandidate":
        if not isinstance(value, dict):
            raise RuleValidationError("release candidate must be an object")
        if value.get("schema") != CANDIDATE_SCHEMA:
            raise RuleValidationError("unsupported candidate schema %r" % (value.get("schema"),))
        by_family = {rule.page_family: rule for rule in rules}
        selected: list[RulePackage] = []
        for item in value.get("rules", []):
            family = item.get("page_family", "")
            rule = by_family.get(family)
            if rule is None:
                raise RuleValidationError("candidate references unregistered family %r" % family)
            if rule.digest != item.get("rule_digest"):
                raise RuleValidationError("candidate digest does not match package for %s" % family)
            selected.append(rule)
        return cls(label=value.get("label", ""), rules=tuple(selected))


@dataclass(frozen=True)
class FamilyThresholds:
    family: str
    min_pages: int
    required_pass_rate: float = 1.0
    max_ambiguity_rate: float = 0.0
    max_gold_conflicts: int = 0
    min_gold_pages: int = 0
    min_live_pages: int = 0
    max_diff_rate: float | None = None

    def __post_init__(self) -> None:
        if not _FAMILY_RE.fullmatch(self.family or ""):
            raise RuleValidationError("invalid threshold family %r" % (self.family,))
        if self.min_pages < 1:
            raise RuleValidationError("min_pages must be at least 1")
        if self.min_gold_pages < 0:
            raise RuleValidationError("min_gold_pages must not be negative")
        if self.min_live_pages < 0:
            raise RuleValidationError("min_live_pages must not be negative")
        if self.max_gold_conflicts < 0:
            raise RuleValidationError("max_gold_conflicts must not be negative")
        for name in ("required_pass_rate", "max_ambiguity_rate"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 1:
                raise RuleValidationError("%s must be between 0 and 1" % name)
        if self.max_diff_rate is not None and (
            not isinstance(self.max_diff_rate, (int, float))
            or isinstance(self.max_diff_rate, bool)
            or not 0 <= self.max_diff_rate <= 1
        ):
            raise RuleValidationError("max_diff_rate must be between 0 and 1")

    def to_json(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "min_pages": self.min_pages,
            "min_gold_pages": self.min_gold_pages,
            "min_live_pages": self.min_live_pages,
            "required_pass_rate": self.required_pass_rate,
            "max_ambiguity_rate": self.max_ambiguity_rate,
            "max_gold_conflicts": self.max_gold_conflicts,
            "max_diff_rate": self.max_diff_rate,
        }

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> "FamilyThresholds":
        if not isinstance(value, dict):
            raise RuleValidationError("family threshold must be an object")
        return cls(
            family=value.get("family", ""),
            min_pages=value.get("min_pages", 0),
            required_pass_rate=value.get("required_pass_rate", 1.0),
            max_ambiguity_rate=value.get("max_ambiguity_rate", 0.0),
            max_gold_conflicts=value.get("max_gold_conflicts", 0),
            min_gold_pages=value.get("min_gold_pages", 0),
            min_live_pages=value.get("min_live_pages", 0),
            max_diff_rate=value.get("max_diff_rate"),
        )


@dataclass(frozen=True)
class ReleaseConfig:
    thresholds: tuple[FamilyThresholds, ...]
    circuit_failure_threshold: int = 3
    schema: str = CONFIG_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != CONFIG_SCHEMA:
            raise RuleValidationError("unsupported release config schema")
        families = [item.family for item in self.thresholds]
        if len(families) != len(set(families)):
            raise RuleValidationError("duplicate family thresholds")
        if self.circuit_failure_threshold < 1:
            raise RuleValidationError("circuit_failure_threshold must be at least 1")

    def for_family(self, family: str) -> FamilyThresholds | None:
        return next((item for item in self.thresholds if item.family == family), None)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "circuit_failure_threshold": self.circuit_failure_threshold,
            "thresholds": [item.to_json() for item in self.thresholds],
        }

    @property
    def digest(self) -> str:
        return sha256_json(self.to_json())

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> "ReleaseConfig":
        if not isinstance(value, dict):
            raise RuleValidationError("release config must be an object")
        raw = value.get("thresholds", [])
        if not isinstance(raw, list):
            raise RuleValidationError("thresholds must be a list")
        return cls(
            thresholds=tuple(FamilyThresholds.from_json(item) for item in raw),
            circuit_failure_threshold=value.get("circuit_failure_threshold", 3),
            schema=value.get("schema", CONFIG_SCHEMA),
        )


__all__ = [
    "CANDIDATE_SCHEMA",
    "CONFIG_SCHEMA",
    "FamilyThresholds",
    "FieldRule",
    "ReleaseCandidate",
    "ReleaseConfig",
    "RULE_SCHEMA",
    "RulePackage",
    "RuleValidationError",
    "SemanticConstraint",
    "canonical_json",
    "is_digest",
    "sha256_bytes",
    "sha256_json",
]
