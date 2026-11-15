"""Capture collector for the local supplier drill service.

Captures preserve the raw response bytes, the URL, the page family, the
response digest and the source tag.  Live captures may carry a sample
ordinal, which is what a release window binds.  Nothing here parses the page:
evidence is produced later by one pinned extractor version.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .store import Capture, Store

SOURCE_ARCHIVE = "archive"
SOURCE_LIVE = "live"


class CollectorError(RuntimeError):
    """The supplier response could not be captured faithfully."""


@dataclass
class SupplierClient:
    base_url: str
    timeout: float = 5.0

    def fetch(self, path: str) -> tuple[bytes, dict[str, str], str]:
        url = self.base_url.rstrip("/") + "/" + path.lstrip("/")
        request = Request(url, headers={"User-Agent": "extractor-rollout-drill/1.0"})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read()
                headers = {key.lower(): value for key, value in response.headers.items()}
                final_url = response.geturl()
        except (HTTPError, URLError, OSError) as error:
            raise CollectorError("fetch %s failed: %s" % (url, error)) from error
        if not body:
            raise CollectorError("fetch %s returned an empty body" % url)
        return body, headers, final_url


def capture_page(
    store: Store,
    client: SupplierClient,
    path: str,
    page_family: str,
    source_tag: str,
    sample_ordinal: int | None = None,
) -> Capture:
    body, headers, url = client.fetch(path)
    return store.capture(
        url=url,
        page_family=page_family,
        body=body,
        headers=headers,
        source_tag=source_tag,
        sample_ordinal=sample_ordinal,
    )


def capture_many(
    store: Store,
    client: SupplierClient,
    entries: list[dict[str, Any]],
) -> list[Capture]:
    captures = []
    for entry in entries:
        captures.append(
            capture_page(
                store,
                client,
                entry["path"],
                entry["page_family"],
                entry["source"],
                entry.get("sample_ordinal"),
            )
        )
    return captures


def control(base_url: str, state: dict[str, Any]) -> None:
    url = base_url.rstrip("/") + "/__control"
    request = Request(
        url,
        data=json.dumps(state).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=5) as response:
        response.read()


__all__ = [
    "CollectorError",
    "SOURCE_ARCHIVE",
    "SOURCE_LIVE",
    "SupplierClient",
    "capture_many",
    "capture_page",
    "control",
]
