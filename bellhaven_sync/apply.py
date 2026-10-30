"""Write an approved proposal to the CRM.

Approval happened against a snapshot, so before writing we re-read the live
account and refuse if anything we based the decision on has moved. The
billing SOP is re-checked here too, not just at proposal time.
"""
from __future__ import annotations

from datetime import date

from . import config, normalize as N
from .crm import CRM
from .match import has_billing_history

# Fields that, if changed since the proposal was made, invalidate it.
GUARDED = ["name", "parent_id", "status", "billing_street", "billing_city", "billing_zip",
           "lifetime_revenue", "outstanding_ar", "chow_current_account", "duplicate_of_account"]


class Stale(Exception):
    pass


def _check_fresh(live: dict, snapshot: dict):
    drift = [f for f in GUARDED if live.get(f) != snapshot.get(f)]
    if drift:
        raise Stale(f"account changed since proposal ({', '.join(drift)}); re-run the pipeline")


def _note(existing: str, text: str) -> str:
    line = f"{config.NOTE_TAG} {date.today().isoformat()}: {text}"
    return f"{existing}\n{line}" if existing else line


def _find_existing(crm: CRM, body: dict) -> dict | None:
    """Guard against double-creates (e.g. a retry after a partial failure)."""
    for a in crm.accounts(city=body["billing_city"], state=body["billing_state"]):
        if (a["parent_id"] == body["parent_id"] and not a["duplicate_of_account"]
                and N.street(a["billing_street"]) == N.street(body["billing_street"])):
            return a
    return None


def _create(crm: CRM, body: dict, note: str) -> dict:
    existing = _find_existing(crm, body)
    if existing:
        return existing
    return crm.create_account({**body, "note": _note("", note) if note else ""})


def apply(crm: CRM, p: dict) -> dict:
    """Returns a result dict for the audit trail. Raises on refusal/failure."""
    kind, changes = p["kind"], dict(p["changes"])

    if kind == "create":
        new = _create(crm, p["new_account"], p["note"])
        return {"created": new["account_id"]}

    live = crm.account(p["account_id"])
    _check_fresh(live, p["snapshot"])

    if kind == "chow":
        if not has_billing_history(live):
            raise Stale("account no longer has revenue + open AR; re-run to get a direct re-parent instead")
        new = _create(crm, p["new_account"], p["note"])  # reuses the account if a prior attempt created it
        crm.update_account(live["account_id"], {"chow_current_account": new["account_id"]})
        return {"created": new["account_id"], "chow_from": live["account_id"]}

    if "parent_id" in changes and has_billing_history(live):
        raise Stale("billing SOP: account has revenue + open AR, must go through CHOW, not a re-parent")

    if p["note"]:
        changes["note"] = _note(live["note"], p["note"])
    crm.update_account(live["account_id"], changes)
    return {"updated": live["account_id"], "fields": sorted(changes)}
