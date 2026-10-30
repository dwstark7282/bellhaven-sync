"""Daily run: scrape -> read CRM -> match -> enqueue. Never writes to the CRM.

    python -m bellhaven_sync.pipeline
"""
from __future__ import annotations

import json
import sys

from . import match, store
from .crm import CRM
from .scrape import scrape


def run(db=None, crm: CRM | None = None) -> dict:
    db, crm, started = db or store.connect(), crm or CRM(), store.now()
    locations = scrape()
    if not locations:
        raise RuntimeError("scraped 0 locations; refusing to treat every account as unlisted")
    accounts, contacts = crm.accounts(), crm.contacts()
    proposals, matched = match.build(locations, accounts, contacts)
    queue = store.enqueue(db, proposals)
    summary = {"locations": len(locations), "accounts": len(accounts), "confident_matches": len(matched),
               "proposals": len(proposals), "queue": queue, "matched": matched}
    store.record_run(db, started, summary)
    return summary


if __name__ == "__main__":
    s = run()
    print(json.dumps({k: v for k, v in s.items() if k != "matched"}, indent=2))
    sys.exit(0)
