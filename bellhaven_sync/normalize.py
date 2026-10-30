"""Normalization used for comparing website records to CRM records.

Comparison only: we never write normalized values back to the CRM.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from . import config

STREET_ABBREV = {
    "street": "st", "avenue": "ave", "road": "rd", "drive": "dr", "lane": "ln",
    "boulevard": "blvd", "pike": "pike", "pk": "pike", "court": "ct", "place": "pl",
    "highway": "hwy", "parkway": "pkwy", "circle": "cir", "terrace": "ter",
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northwest": "nw", "northeast": "ne", "southwest": "sw", "southeast": "se",
}

NAME_ABBREV = {"rehab": "rehabilitation", "and": "&", "centre": "center", "healthcare": "health care"}


def _tokens(s: str) -> list[str]:
    return re.sub(r"[^a-z0-9& ]", " ", (s or "").lower().replace("&amp;", "&")).split()


def street(s: str) -> str:
    return " ".join(STREET_ABBREV.get(t, t) for t in _tokens(s))


def is_po_box(s: str) -> bool:
    return bool(re.match(r"\s*p\.?\s*o\.?\s*box\b", s or "", re.I))


def name(s: str) -> str:
    out = " ".join(NAME_ABBREV.get(t, t) for t in _tokens(s))
    return re.sub(r"^the ", "", out)


def exact(s: str) -> str:
    """Case/whitespace-insensitive. Used for names, where the website spelling is the official brand."""
    return " ".join((s or "").replace("&amp;", "&").lower().split())


def name_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, name(a), name(b)).ratio()


def phone(s: str) -> str:
    digits = re.sub(r"\D", "", s or "")
    return digits[-10:]


def zip5(s: str) -> str:
    return (s or "").strip()[:5]


def city(s: str) -> str:
    return " ".join(_tokens(s))


def care_types(offerings: list[str]) -> list[str]:
    """Website offerings mapped to CRM picklist values, order preserved."""
    out = []
    for o in offerings:
        mapped = config.CARE_TYPE_MAP.get(o.lower().replace("&amp;", "&").strip())
        if mapped and mapped not in out:
            out.append(mapped)
    return out
