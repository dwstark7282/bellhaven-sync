"""Scrape every Bellhaven community from the public website.

The directory is paginated, and some communities (e.g. newly acquired ones)
are linked only from the home page, so we collect detail links from the home
page and every directory page, then parse each detail page.
"""
from __future__ import annotations

import html
import re
from dataclasses import asdict, dataclass, field

import requests

from . import config


@dataclass
class Location:
    slug: str
    name: str
    street: str
    city: str
    state: str
    zip: str
    care_offerings: list[str] = field(default_factory=list)
    administrator: str = ""
    phone: str = ""
    url: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _get(session: requests.Session, path: str) -> str:
    resp = session.get(config.BASE_URL + path, timeout=30)
    resp.raise_for_status()
    return resp.text


def _detail_slugs(page_html: str) -> list[str]:
    return re.findall(r'href="/communities/([^"?#/]+)"', page_html)


def _text(fragment: str) -> str:
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment))).strip()


def parse_detail(slug: str, page_html: str) -> Location:
    title = re.search(r"<h1[^>]*>(.*?)</h1>", page_html, re.S)
    dl = re.search(r'<dl class="detail">(.*?)</dl>', page_html, re.S)
    if not title or not dl:
        raise ValueError(f"unexpected layout on /communities/{slug}")

    sections: dict[str, list[str]] = {}
    for dt, dd in re.findall(r"<dt>(.*?)</dt>\s*<dd>(.*?)</dd>", dl.group(1), re.S):
        # Multi-value fields (care offerings) render as badges or <br>-separated lines.
        parts = re.split(r"<br\s*/?>|</span>", dd)
        sections[_text(dt).lower()] = [p for p in (_text(x) for x in parts) if p]

    address = sections.get("address", [])
    if len(address) < 2:
        raise ValueError(f"no address on /communities/{slug}")
    m = re.match(r"(.+?),\s*([A-Z]{2})\s+(\d{5})", address[-1])
    if not m:
        raise ValueError(f"unparseable city line {address[-1]!r} on /communities/{slug}")

    return Location(
        slug=slug,
        name=_text(title.group(1)),
        street=" ".join(address[:-1]),
        city=m.group(1),
        state=m.group(2),
        zip=m.group(3),
        care_offerings=sections.get("care offerings", []),
        administrator=" ".join(sections.get("administrator", [])),
        phone=" ".join(sections.get("phone", [])),
        url=f"{config.BASE_URL}/communities/{slug}",
    )


def scrape(session: requests.Session | None = None) -> list[Location]:
    session = session or requests.Session()
    slugs = _detail_slugs(_get(session, "/"))

    seen_pages, next_page = set(), "/communities"
    while next_page and next_page not in seen_pages:
        seen_pages.add(next_page)
        page = _get(session, next_page)
        slugs += _detail_slugs(page)
        nxt = re.search(r'href="(/communities\?page=\d+)"[^>]*>\s*Next', page)
        next_page = nxt.group(1) if nxt else None

    return [parse_detail(s, _get(session, f"/communities/{s}")) for s in dict.fromkeys(slugs)]
