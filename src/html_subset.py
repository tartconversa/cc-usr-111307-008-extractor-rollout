"""A deliberately small, documented HTML locator subset.

The subset is *not* browser CSS.  A rule may use only the syntax below; any
other syntax is rejected when the rule is registered, rather than being
silently given a different meaning.

Supported
---------
* type selectors (``div``) and the universal selector (``*``)
* ids (``#resource``) and classes (``.provider``)
* attribute presence and comparisons:
  ``[data-role]``, ``[data-role=main]``, ``[class~=provider]``,
  ``[href^=https]``, ``[href$=.html]``, ``[href*=example]``
* descendant (space) and direct-child (``>``) combinators
* comma-separated selector lists (a union, in document order)

Rejected
--------
Sibling combinators, pseudo-classes, pseudo-elements, namespaces, CSS
escapes and comments.  A rule that needs them must be revised; the extractor
will not approximate browser behaviour.

The tree is built with the standard library's tolerant ``HTMLParser``.  Text
extraction ignores ``script``, ``style`` and ``template`` and collapses
whitespace runs.  Element evidence records the node path, line and column,
attributes and text so a review decision can be traced back to the raw bytes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from html.parser import HTMLParser
import re
from typing import Iterable, Iterator

VOID_ELEMENTS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
TEXT_IGNORED_ELEMENTS = frozenset({"script", "style", "template"})
_WS = re.compile(r"\s+")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
_ATTR_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_:-]*")

SELECTOR_SUBSET_SUMMARY = (
    "type/universal/id/class selectors; [attr], [attr=value], "
    "[attr~=value], [attr^=value], [attr$=value], [attr*=value]; "
    "descendant and > combinators; comma unions. No sibling combinators, "
    "pseudo-classes, pseudo-elements, namespaces, escapes or comments."
)


class LocatorSyntaxError(ValueError):
    """Raised when a locator uses syntax outside the documented subset."""


@dataclass
class Element:
    tag: str
    attrs: dict[str, str]
    line: int
    column: int
    parent: "Element | None" = None
    children: list["Element | str"] = field(default_factory=list)
    ordinal: int = 0

    def ancestors(self) -> Iterator["Element"]:
        node = self.parent
        while node is not None:
            yield node
            node = node.parent

    def path(self) -> str:
        """A stable, human-readable path used as node evidence."""

        parts: list[str] = []
        node: Element | None = self
        while node is not None and node.parent is not None:
            part = node.tag
            if "id" in node.attrs:
                part += "#" + node.attrs["id"]
            classes = node.attrs.get("class", "").split()
            if classes:
                part += "." + ".".join(classes)
            # Distinguish same-tag siblings without pretending to be CSS.
            same_tag = [c for c in node.parent.children if isinstance(c, Element) and c.tag == node.tag]
            part += "[%d]" % (same_tag.index(node) + 1)
            parts.append(part)
            node = node.parent
        return "/".join(reversed(parts)) or self.tag

    def text(self) -> str:
        chunks: list[str] = []

        def walk(node: Element) -> None:
            if node.tag in TEXT_IGNORED_ELEMENTS:
                return
            for child in node.children:
                if isinstance(child, Element):
                    walk(child)
                else:
                    chunks.append(child)

        walk(self)
        return _WS.sub(" ", "".join(chunks)).strip()

    def descendant_or_self(self) -> Iterator["Element"]:
        stack = [self]
        while stack:
            node = stack.pop()
            yield node
            for child in reversed(node.children):
                if isinstance(child, Element):
                    stack.append(child)


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Element("#document", {}, 1, 0)
        self.stack = [self.root]
        self.ordinal = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        node = Element(
            tag,
            {name.lower(): value if value is not None else "" for name, value in attrs},
            *self.getpos(),
        )
        parent = self.stack[-1]
        node.parent = parent
        node.ordinal = self.ordinal
        self.ordinal += 1
        parent.children.append(node)
        if tag not in VOID_ELEMENTS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        node = Element(
            tag,
            {name.lower(): value if value is not None else "" for name, value in attrs},
            *self.getpos(),
        )
        parent = self.stack[-1]
        node.parent = parent
        node.ordinal = self.ordinal
        self.ordinal += 1
        parent.children.append(node)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in VOID_ELEMENTS:
            return
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return
        # Unmatched end tag: ignore it, as a tolerant HTML parser would.

    def handle_data(self, data):
        if data:
            self.stack[-1].children.append(data)


@dataclass(frozen=True)
class AttributeTest:
    name: str
    op: str | None
    value: str | None

    def matches(self, element: Element) -> bool:
        if self.name not in element.attrs:
            return False
        if self.op is None:
            return True
        actual = element.attrs[self.name]
        if self.op == "=":
            return actual == self.value
        if self.op == "~=":
            return self.value in actual.split()
        if self.op == "^=":
            return actual.startswith(self.value)
        if self.op == "$=":
            return actual.endswith(self.value)
        if self.op == "*=":
            return self.value in actual
        raise AssertionError("unreachable attribute operator")


@dataclass(frozen=True)
class Compound:
    tag: str | None
    ids: tuple[str, ...]
    classes: tuple[str, ...]
    attributes: tuple[AttributeTest, ...]

    def matches(self, element: Element) -> bool:
        if self.tag not in (None, "*") and element.tag != self.tag:
            return False
        if self.ids:
            if element.attrs.get("id") not in self.ids:
                return False
        if self.classes:
            classes = set(element.attrs.get("class", "").split())
            if not set(self.classes).issubset(classes):
                return False
        return all(test.matches(element) for test in self.attributes)


@dataclass(frozen=True)
class ComplexSelector:
    # Left-to-right compounds.  ``combinators[i]`` joins compounds[i] to
    # compounds[i + 1] and is either ``" "`` or ``">"``.
    compounds: tuple[Compound, ...]
    combinators: tuple[str, ...]

    def matches(self, element: Element) -> bool:
        return self._match_from(len(self.compounds) - 1, element)

    def _match_from(self, index: int, element: Element) -> bool:
        if not self.compounds[index].matches(element):
            return False
        if index == 0:
            return True
        combinator = self.combinators[index - 1]
        if combinator == ">":
            return element.parent is not None and self._match_from(index - 1, element.parent)
        for ancestor in element.ancestors():
            if self._match_from(index - 1, ancestor):
                return True
        return False


class _SelectorParser:
    def __init__(self, source: str) -> None:
        self.source = source
        self.pos = 0

    def error(self, message: str) -> LocatorSyntaxError:
        return LocatorSyntaxError("%s at offset %d in %r" % (message, self.pos, self.source))

    def skip_ws(self) -> bool:
        start = self.pos
        while self.pos < len(self.source) and self.source[self.pos].isspace():
            self.pos += 1
        return self.pos > start

    def peek(self) -> str:
        return self.source[self.pos] if self.pos < len(self.source) else ""

    def attribute(self) -> AttributeTest:
        # current char is '['
        self.pos += 1
        self.skip_ws()
        match = _ATTR_NAME_RE.match(self.source, self.pos)
        if not match:
            raise self.error("expected an attribute name")
        name = match.group(0).lower()
        self.pos = match.end()
        self.skip_ws()
        if self.peek() == "]":
            self.pos += 1
            return AttributeTest(name, None, None)
        op = None
        for candidate in ("~=", "^=", "$=", "*=", "="):
            if self.source.startswith(candidate, self.pos):
                op = candidate
                self.pos += len(candidate)
                break
        if op is None:
            raise self.error("expected an attribute comparison operator")
        self.skip_ws()
        if self.peek() in ("'", '"'):
            quote = self.peek()
            self.pos += 1
            start = self.pos
            while self.pos < len(self.source) and self.source[self.pos] != quote:
                if self.source[self.pos] == "\\":
                    raise self.error("CSS escapes are not supported")
                self.pos += 1
            if self.peek() != quote:
                raise self.error("unterminated attribute value")
            value = self.source[start:self.pos]
            self.pos += 1
        else:
            start = self.pos
            while self.pos < len(self.source) and self.source[self.pos] not in " \t\r\n]":
                if self.source[self.pos] in "'\"":
                    raise self.error("unexpected quote in unquoted attribute value")
                self.pos += 1
            value = self.source[start:self.pos]
            if not value:
                raise self.error("expected an attribute value")
        self.skip_ws()
        if self.peek() != "]":
            raise self.error("expected ']'")
        self.pos += 1
        return AttributeTest(name, op, value)

    def compound(self) -> Compound:
        tag = None
        ids: list[str] = []
        classes: list[str] = []
        attrs: list[AttributeTest] = []
        if self.peek() == "*":
            self.pos += 1
            tag = "*"
        else:
            match = _IDENT_RE.match(self.source, self.pos)
            if match:
                tag = match.group(0).lower()
                self.pos = match.end()
        while self.pos < len(self.source):
            char = self.peek()
            if char == "#":
                self.pos += 1
                match = _IDENT_RE.match(self.source, self.pos)
                if not match:
                    raise self.error("expected an id after '#'")
                ids.append(match.group(0))
                self.pos = match.end()
            elif char == ".":
                self.pos += 1
                match = _IDENT_RE.match(self.source, self.pos)
                if not match:
                    raise self.error("expected a class after '.'")
                classes.append(match.group(0))
                self.pos = match.end()
            elif char == "[":
                attrs.append(self.attribute())
            elif char in ":+~|\\":
                raise self.error("unsupported selector syntax %r" % char)
            else:
                break
        if tag is None and not ids and not classes and not attrs:
            raise self.error("empty compound selector")
        return Compound(tag, tuple(ids), tuple(classes), tuple(attrs))

    def complex(self) -> ComplexSelector:
        compounds = [self.compound()]
        combinators: list[str] = []
        while True:
            had_ws = self.skip_ws()
            if self.pos >= len(self.source) or self.peek() == ",":
                break
            if self.peek() == ">":
                self.pos += 1
                self.skip_ws()
                combinators.append(">")
                compounds.append(self.compound())
            elif had_ws:
                combinators.append(" ")
                compounds.append(self.compound())
            else:
                raise self.error("expected whitespace, '>' or ','")
        return ComplexSelector(tuple(compounds), tuple(combinators))

    def parse(self) -> tuple[ComplexSelector, ...]:
        if not self.source.strip():
            raise self.error("empty selector")
        selectors = [self.complex()]
        self.skip_ws()
        while self.peek() == ",":
            self.pos += 1
            self.skip_ws()
            selectors.append(self.complex())
            self.skip_ws()
        if self.pos != len(self.source):
            raise self.error("unexpected trailing characters")
        return tuple(selectors)


@lru_cache(maxsize=512)
def parse_selector(source: str) -> tuple[ComplexSelector, ...]:
    """Parse a selector list into the documented subset or raise.

    Parsed selectors are cached.  Callers should validate at rule-registration
    time so a malformed locator never reaches a live crawl.
    """

    return _SelectorParser(source).parse()


class Document:
    def __init__(self, elements: list[Element]) -> None:
        self.elements = elements

    @classmethod
    def parse(cls, html: str | bytes) -> "Document":
        if isinstance(html, bytes):
            html = html.decode("utf-8", "replace")
        builder = _TreeBuilder()
        builder.feed(html)
        builder.close()
        elements = list(builder.root.descendant_or_self())
        # Remove the synthetic #document root from the queryable set.
        elements = [element for element in elements if element.tag != "#document"]
        return cls(elements)

    def select(self, selector: str) -> list[Element]:
        selectors = parse_selector(selector)
        return [element for element in self.elements if any(item.matches(element) for item in selectors)]

    def select_first(self, selector: str) -> Element | None:
        matches = self.select(selector)
        return matches[0] if matches else None


def is_within(element: Element, scope_elements: Iterable[Element]) -> bool:
    scopes = tuple(scope_elements)
    if not scopes:
        return False
    for node in [element, *element.ancestors()]:
        if any(node is scope for scope in scopes):
            return True
    return False


__all__ = [
    "ComplexSelector",
    "Document",
    "Element",
    "LocatorSyntaxError",
    "SELECTOR_SUBSET_SUMMARY",
    "is_within",
    "parse_selector",
]
