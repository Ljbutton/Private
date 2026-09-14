"""RSS aggregation across the mainstream NFL outlets.

Deliberately dependency-free: ``xml.etree`` handles RSS 2.0 and Atom well
enough, and avoiding feedparser keeps the install small.
"""

from __future__ import annotations

import re
from xml.etree import ElementTree

from ..util import iso, stable_id
from .base import HttpClient, SourceError

FEEDS: list[tuple[str, str]] = [
    ("ESPN", "https://www.espn.com/espn/rss/nfl/news"),
    ("ProFootballTalk", "https://profootballtalk.nbcsports.com/feed/"),
    ("CBS Sports", "https://www.cbssports.com/rss/headlines/nfl/"),
    ("Yahoo Sports", "https://sports.yahoo.com/nfl/rss.xml"),
    ("NFL.com", "https://www.nfl.com/feeds/rss/news"),
]

_TAG_RE = re.compile(r"<[^>]+>")
_ATOM = "{http://www.w3.org/2005/Atom}"


def _clean(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", text)).strip()


def parse_feed(xml_text: str, source: str) -> list[dict]:
    try:
        root = ElementTree.fromstring(xml_text.strip())
    except ElementTree.ParseError:
        return []

    items: list[dict] = []
    nodes = root.findall(".//item") or root.findall(f".//{_ATOM}entry")
    for node in nodes:
        title = _clean(
            node.findtext("title") or node.findtext(f"{_ATOM}title") or ""
        )
        if not title:
            continue
        link = node.findtext("link") or ""
        if not link:
            atom_link = node.find(f"{_ATOM}link")
            if atom_link is not None:
                link = atom_link.get("href") or ""
        published = (
            node.findtext("pubDate")
            or node.findtext(f"{_ATOM}published")
            or node.findtext(f"{_ATOM}updated")
            or node.findtext("{http://purl.org/dc/elements/1.1/}date")
        )
        summary = _clean(
            node.findtext("description")
            or node.findtext(f"{_ATOM}summary")
            or node.findtext(f"{_ATOM}content")
        )
        items.append(
            {
                "id": stable_id(source, link or title),
                "source": source,
                "title": title,
                "url": link.strip(),
                "summary": summary[:600],
                "published_at": iso(published),
            }
        )
    return items


class NewsSource:
    def __init__(self, client: HttpClient | None = None, feeds=None) -> None:
        self.http = client or HttpClient(cache_ttl=300.0)
        self.feeds = feeds if feeds is not None else FEEDS

    def fetch(self) -> list[dict]:
        """Pull every feed; a dead feed costs its own items, nothing more."""
        items: list[dict] = []
        seen: set[str] = set()
        for source, url in self.feeds:
            try:
                xml_text = self.http.get_text(url, retries=2)
            except SourceError:
                continue
            for item in parse_feed(xml_text, source):
                if item["id"] in seen:
                    continue
                seen.add(item["id"])
                items.append(item)
        return items
