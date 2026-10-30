"""Match website locations to CRM accounts and turn findings into proposals.

Pure functions over plain dicts: no network, no database. That keeps the
logic testable against a frozen snapshot (tests/fixtures).

Proposal kinds
  update        matched account needs field fixes and/or a direct re-parent
  chow          matched account must move parent but has revenue AND open AR:
                leave it untouched, create a new account under Bellhaven,
                point old.chow_current_account at the new one (billing SOP)
  create        location on the website with no CRM account
  duplicate     second CRM account for the same facility -> Inactive + duplicate_of
  sold          Bellhaven account gone from the site, same facility exists under
                another parent -> CHOW link (revenue + AR) or duplicate_of
  not_listed    Bellhaven account gone from the site, no other evidence -> Needs Review
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from . import config, normalize as N


@dataclass
class Proposal:
    kind: str
    title: str
    account_id: str = ""          # account being changed (empty for create)
    slug: str = ""                # website location, if any
    changes: dict = field(default_factory=dict)        # PATCH fields for account_id
    new_account: dict = field(default_factory=dict)    # POST body for create / chow
    note: str = ""                # appended to the account's note on apply
    evidence: dict = field(default_factory=dict)
    snapshot: dict = field(default_factory=dict)       # account as seen at proposal time

    @property
    def fingerprint(self) -> str:
        """Stable id: the same finding on a later run hashes the same, so a
        decided proposal is never re-queued. Notes and evidence are excluded
        on purpose because they are presentation, not the decision."""
        key = {"kind": self.kind, "account": self.account_id, "slug": self.slug,
               "changes": self.changes,
               "new": {k: v for k, v in self.new_account.items() if k != "note"}}
        return hashlib.sha1(json.dumps(key, sort_keys=True).encode()).hexdigest()[:16]


def has_billing_history(acct: dict) -> bool:
    """Billing SOP: revenue history AND outstanding AR means preserve the old account."""
    return (acct.get("lifetime_revenue") or 0) > 0 and (acct.get("outstanding_ar") or 0) > 0


def same_address(acct: dict, loc_street: str, loc_city: str, loc_state: str) -> bool:
    return (acct["billing_state"] == loc_state
            and N.city(acct["billing_city"]) == N.city(loc_city)
            and N.street(acct["billing_street"]) == N.street(loc_street))


def signals(acct: dict, loc, admins: dict) -> dict:
    return {
        "address_match": same_address(acct, loc.street, loc.city, loc.state),
        "zip_match": N.zip5(acct["billing_zip"]) == loc.zip,
        "phone_match": N.phone(acct["phone"]) == N.phone(loc.phone),
        "name_similarity": round(N.name_similarity(acct["name"], loc.name), 2),
        "administrator_on_file": loc.administrator in admins.get(acct["account_id"], []),
    }


def is_candidate(acct: dict, loc, sig: dict) -> bool:
    if sig["address_match"]:
        return True
    # Address can be junk (PO box, typo) -- then require the same city plus a
    # second independent identifier.
    same_city = acct["billing_state"] == loc.state and N.city(acct["billing_city"]) == N.city(loc.city)
    return same_city and (sig["phone_match"] or sig["administrator_on_file"] or sig["name_similarity"] >= 0.95)


def survivor_rank(acct: dict, sig: dict, target_parent: str, contact_counts: dict):
    """Which of several accounts for one facility to keep. Lower sorts first."""
    return (
        acct["parent_id"] != target_parent,
        acct["status"] != "Active",
        -(acct.get("lifetime_revenue") or 0),
        -contact_counts.get(acct["account_id"], 0),
        not sig["administrator_on_file"],
        not sig["phone_match"],
        -sig["name_similarity"],
        acct["account_id"],
    )


def site_fields(loc) -> dict:
    return {"name": loc.name, "billing_street": loc.street, "billing_city": loc.city,
            "billing_state": loc.state, "billing_zip": loc.zip, "phone": loc.phone}


def field_fixes(acct: dict, loc) -> dict:
    wanted, fixes = site_fields(loc), {}
    compare = {"name": N.exact, "billing_street": N.street, "billing_city": N.city,
               "billing_state": str.upper, "billing_zip": N.zip5, "phone": N.phone}
    for f in config.SYNC_FIELDS:
        if f == "billing_street" and N.is_po_box(acct[f]):
            fixes[f] = wanted[f]
        elif compare[f](acct[f] or "") != compare[f](wanted[f]):
            fixes[f] = wanted[f]
    care = N.care_types(loc.care_offerings)
    if care and acct["care_type"] not in care:
        fixes["care_type"] = care[0]
    if acct["status"] != "Active":
        fixes["status"] = "Active"
    return fixes


def new_account_body(loc, parent_id: str) -> dict:
    care = N.care_types(loc.care_offerings)
    return {**site_fields(loc), "parent_id": parent_id, "status": "Active",
            "care_type": care[0] if care else ""}


def build(locations, accounts: list[dict], contacts: list[dict]) -> tuple[list[Proposal], list[dict]]:
    """Returns (proposals, confident_matches)."""
    by_id = {a["account_id"]: a for a in accounts}
    parents = {a["account_id"]: a for a in accounts if a["name"].endswith("(Parent Account)")}
    target = next(pid for pid, a in parents.items() if a["name"] == config.TARGET_PARENT_NAME)
    parent_name = lambda pid: by_id.get(pid, {}).get("name", "(no parent)") if pid else "(no parent)"

    admins, contact_counts = {}, {}
    for c in contacts:
        if c.get("is_active"):
            admins.setdefault(c["account_id"], []).append(c["name"])
            contact_counts[c["account_id"]] = contact_counts.get(c["account_id"], 0) + 1

    # Accounts already resolved by an earlier decision are out of the pool:
    # duplicates point at their survivor, CHOW'd accounts point at their replacement.
    pool = [a for a in accounts if a["account_id"] not in parents
            and not a["duplicate_of_account"] and not a["chow_current_account"]
            and "UNDONE: created in error" not in (a["note"] or "")]

    proposals, matched, claimed = [], [], set()

    for loc in locations:
        scored = [(a, signals(a, loc, admins)) for a in pool]
        cands = [(a, s) for a, s in scored if is_candidate(a, loc, s) and a["account_id"] not in claimed]
        site_ev = loc.to_dict()

        if not cands:
            nearby = sorted(
                ({"account_id": a["account_id"], "name": a["name"], "street": a["billing_street"],
                  "phone": a["phone"], "parent": a["parent_name"] or "(no parent)",
                  "name_similarity": s["name_similarity"]}
                 for a, s in scored if a["billing_state"] == loc.state
                 and N.city(a["billing_city"]) == N.city(loc.city)),
                key=lambda r: -r["name_similarity"])
            proposals.append(Proposal(
                kind="create", slug=loc.slug, title=f"Create {loc.name} ({loc.city}, {loc.state})",
                new_account=new_account_body(loc, target),
                note=f"Created from website listing {loc.url}",
                evidence={"website": site_ev,
                          "reason": "No CRM account at this address or matching phone/name in this city.",
                          "same_city_accounts": nearby}))
            continue

        cands.sort(key=lambda x: survivor_rank(x[0], x[1], target, contact_counts))
        keep, keep_sig = cands[0]
        claimed.update(a["account_id"] for a, _ in cands)
        cand_ev = [{"account": a, "signals": s, "contacts": admins.get(a["account_id"], [])} for a, s in cands]

        # The survivor.
        fixes = field_fixes(keep, loc)
        moving = keep["parent_id"] != target
        base_ev = {"website": site_ev, "crm": keep, "signals": keep_sig,
                   "contacts": admins.get(keep["account_id"], []), "candidates": cand_ev}
        if moving and has_billing_history(keep):
            proposals.append(Proposal(
                kind="chow", account_id=keep["account_id"], slug=loc.slug, snapshot=keep,
                title=f"CHOW {keep['name']} -> new {loc.name} under Bellhaven",
                changes={"chow_current_account": "<new account id>"},
                new_account=new_account_body(loc, target),
                note=f"Change of ownership from {parent_name(keep['parent_id'])}; "
                     f"prior account {keep['account_id']} kept for billing.",
                evidence={**base_ev, "reason":
                          f"Listed on the Bellhaven website but parented to {parent_name(keep['parent_id'])}. "
                          f"Lifetime revenue ${keep['lifetime_revenue']:,} and outstanding AR "
                          f"${keep['outstanding_ar']:,}: billing SOP says keep the old account untouched, "
                          f"create a new one under Bellhaven, and link via chow_current_account."}))
        elif moving or fixes:
            if moving:
                fixes["parent_id"] = target
            what = []
            if moving:
                what.append(f"re-parent from {parent_name(keep['parent_id'])}")
            what += [f for f in fixes if f not in ("parent_id",)]
            reason = []
            if moving:
                reason.append(f"Listed on the Bellhaven website but parented to {parent_name(keep['parent_id'])}. "
                              f"Revenue ${keep['lifetime_revenue']:,}, AR ${keep['outstanding_ar']:,}: "
                              f"no open billing history, so re-parent directly.")
            if set(fixes) - {"parent_id"}:
                reason.append("Website values differ from CRM for: " + ", ".join(sorted(set(fixes) - {"parent_id"})) + ".")
            proposals.append(Proposal(
                kind="update", account_id=keep["account_id"], slug=loc.slug, snapshot=keep,
                title=f"{keep['name']}: " + ", ".join(what), changes=fixes,
                note=f"Synced to website listing {loc.url}" if moving else "",
                evidence={**base_ev, "reason": " ".join(reason)}))
        else:
            matched.append({"slug": loc.slug, "name": loc.name, "account_id": keep["account_id"],
                            "signals": keep_sig})

        # Every other account for the same facility is a duplicate of the survivor.
        for dup, dsig in cands[1:]:
            ev = {"website": site_ev, "crm": dup, "survivor": keep, "signals": dsig, "candidates": cand_ev,
                  "contacts": admins.get(dup["account_id"], [])}
            if has_billing_history(dup):
                proposals.append(Proposal(
                    kind="not_listed", account_id=dup["account_id"], slug=loc.slug, snapshot=dup,
                    title=f"{dup['name']}: possible duplicate with open AR -> Needs Review",
                    changes={"status": "Needs Review"},
                    note=f"Possible duplicate of {keep['account_id']} ({keep['name']}) but has open AR; "
                         f"left for billing review.",
                    evidence={**ev, "reason": "Same facility as the survivor, but open AR means we should not "
                                              "deactivate it without billing sign-off."}))
                continue
            proposals.append(Proposal(
                kind="duplicate", account_id=dup["account_id"], slug=loc.slug, snapshot=dup,
                title=f"{dup['name']} ({dup['account_id']}) is a duplicate of {keep['name']} ({keep['account_id']})",
                changes={"duplicate_of_account": keep["account_id"], "status": "Inactive"},
                note=f"Duplicate of {keep['account_id']} ({keep['name']}); same facility as {loc.url}",
                evidence={**ev, "reason": f"Same facility as {keep['account_id']}. Survivor chosen by: Bellhaven "
                                          f"parent, active, revenue, contacts, administrator/phone match."}))

    # Bellhaven accounts that no website location claimed.
    for a in pool:
        if a["parent_id"] != target or a["account_id"] in claimed or a["status"] == "Needs Review":
            continue
        elsewhere = [o for o in pool if o["account_id"] != a["account_id"] and o["parent_id"] != target
                     and same_address(o, a["billing_street"], a["billing_city"], a["billing_state"])]
        ev = {"crm": a, "contacts": admins.get(a["account_id"], []), "same_address_elsewhere": elsewhere}
        if elsewhere:
            other = elsewhere[0]
            owner = parent_name(other["parent_id"])
            if has_billing_history(a):
                proposals.append(Proposal(
                    kind="sold", account_id=a["account_id"], snapshot=a,
                    title=f"{a['name']}: sold to {owner} -> CHOW link to {other['name']}",
                    changes={"chow_current_account": other["account_id"]},
                    evidence={**ev, "reason":
                              f"Not on the Bellhaven website; the same address exists as {other['name']} "
                              f"under {owner}. Revenue ${a['lifetime_revenue']:,} and AR ${a['outstanding_ar']:,}: "
                              f"billing SOP says leave this account untouched and point chow_current_account "
                              f"at the account under the new owner (already exists, so nothing to create)."}))
            else:
                proposals.append(Proposal(
                    kind="sold", account_id=a["account_id"], snapshot=a,
                    title=f"{a['name']}: sold to {owner} -> duplicate of {other['name']}",
                    changes={"duplicate_of_account": other["account_id"], "status": "Inactive"},
                    note=f"No longer on Bellhaven website; facility now {other['name']} ({owner}).",
                    evidence={**ev, "reason": f"Not on the Bellhaven website; the same address already exists "
                                              f"under {owner}. No open billing history, so retire this copy."}))
        else:
            proposals.append(Proposal(
                kind="not_listed", account_id=a["account_id"], snapshot=a,
                title=f"{a['name']}: not on Bellhaven website -> Needs Review",
                changes={"status": "Needs Review"},
                note="Under Bellhaven in CRM but not listed on the Bellhaven website. "
                     "Possibly sold or closed; confirm before re-parenting or deactivating.",
                evidence={**ev, "reason": "No website listing and no other CRM account at this address, so "
                                          "we can't tell sold vs closed. Flag for a human rather than guess."}))

    return proposals, matched
