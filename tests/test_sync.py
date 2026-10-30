import copy
import json
from pathlib import Path

import pytest

from bellhaven_sync import match, store
from bellhaven_sync.apply import Stale, apply
from bellhaven_sync.scrape import Location, parse_detail

FX = Path(__file__).parent / "fixtures"
BELLHAVEN = "0015QAPLGS3FVYEEEM"


@pytest.fixture(scope="module")
def data():
    locs = [Location(**d) for d in json.loads((FX / "locations.json").read_text())]
    accts = json.loads((FX / "accounts.json").read_text())
    contacts = json.loads((FX / "contacts.json").read_text())
    return locs, accts, contacts


@pytest.fixture(scope="module")
def result(data):
    return match.build(*data)


def by_account(proposals, account_id):
    return [p for p in proposals if p.account_id == account_id]


def test_scrape_includes_home_page_only_community(data):
    slugs = {l.slug for l in data[0]}
    assert len(slugs) == 35 and "bellhaven-meadows-of-findlay" in slugs


def test_parse_multi_offering():
    html = ('<h1>X of Erie</h1><dl class="detail"><dt>Address</dt><dd>1 A St<br>Erie, PA 16505</dd>'
            '<dt>Care Offerings</dt><dd><span class="badge">Assisted Living</span><span class="badge">Memory Support</span></dd>'
            '<dt>Phone</dt><dd>(1) 2</dd></dl>')
    loc = parse_detail("x", html)
    assert (loc.city, loc.state, loc.zip) == ("Erie", "PA", "16505")
    assert loc.care_offerings == ["Assisted Living", "Memory Support"]


def test_chow_when_revenue_and_open_ar(result):
    props, _ = result
    for acct in ("001U6RW32TY0WSXZZB", "001A34WFSUYHCRBLFT"):  # Tiffin, Marietta (Cedar Trail, rev + AR)
        (p,) = by_account(props, acct)
        assert p.kind == "chow" and p.new_account["parent_id"] == BELLHAVEN
        assert "parent_id" not in p.changes


def test_direct_reparent_when_no_open_ar(result):
    (p,) = by_account(result[0], "001LGFPBJY4N9MB6KL")  # Lima: revenue but AR 0
    assert p.kind == "update" and p.changes["parent_id"] == BELLHAVEN


def test_sold_facility_links_to_new_owner(result):
    (p,) = by_account(result[0], "001SXSF4ELF0Z2LGDM")  # Sandusky -> Millstone, rev + AR
    assert p.kind == "sold" and p.changes == {"chow_current_account": "0017JP8Z1UQ763BVK3"}


def test_three_way_duplicate_collapses_to_bellhaven_copy(result):
    props, _ = result
    dups = [p for p in props if p.kind == "duplicate" and p.changes["duplicate_of_account"] == "001U1750VLVJAGG1S5"]
    assert {p.account_id for p in dups} == {"0011AB44D05WLA9HTX", "00159PL81N38KM4FHM"}  # Monroe


def test_lookalikes_are_not_matched(result):
    props, _ = result
    created = {p.slug for p in props if p.kind == "create"}
    # Carlisle PA != New Carlisle OH; Amberly Manor CO != Hudson OH; Union Square is a different street.
    assert created == {"bellhaven-of-carlisle", "amberly-manor", "bellhaven-at-union-square", "bellhaven-of-batavia"}
    assert not by_account(props, "0019M1Z45PGV2DJ971")  # Maplewood Senior Care Center (Stonebridge)


def test_bad_address_still_matches_on_phone(result):
    (p,) = by_account(result[0], "001NXP9X46CWEPSLSV")  # Ashtabula, PO Box
    assert p.changes == {"billing_street": "3156 W Prospect Rd"}


def test_unlisted_without_evidence_needs_review(result):
    props, _ = result
    for acct in ("0016PVXH4B25HWR7QE", "00116ETS45BL7DTQP7"):  # Coldwater, Alliance
        (p,) = by_account(props, acct)
        assert p.kind == "not_listed" and p.changes == {"status": "Needs Review"}


def test_rerun_does_not_requeue_decided(data):
    db = store.connect(":memory:")
    props, _ = match.build(*data)
    assert store.enqueue(db, props)["new"] == len(props)
    store.decide(db, props[0].fingerprint, "rejected", "t")
    again = store.enqueue(db, match.build(*data)[0])
    assert again["new"] == 0 and again["already_decided"] == 1
    assert len(store.list_by_status(db, "pending")) == len(props) - 1


def test_applied_state_produces_no_new_proposals(data):
    """Simulate the CRM after approvals: the same finding must not come back."""
    locs, accts, contacts = data
    accts = copy.deepcopy(accts)
    lima = next(a for a in accts if a["account_id"] == "001LGFPBJY4N9MB6KL")
    lima.update(parent_id=BELLHAVEN, phone="(231) 539-3617")
    assert not by_account(match.build(locs, accts, contacts)[0], lima["account_id"])


class FakeCRM:
    def __init__(self, accts):
        self.accts = {a["account_id"]: copy.deepcopy(a) for a in accts}
        self.writes = []

    def account(self, i):
        return copy.deepcopy(self.accts[i])

    def accounts(self, **_):
        return list(self.accts.values())

    def update_account(self, i, f):
        self.writes.append(("PATCH", i, f))
        self.accts[i].update(f)

    def create_account(self, body):
        new = {**body, "account_id": f"NEW{len(self.accts)}", "duplicate_of_account": ""}
        self.accts[new["account_id"]] = new
        self.writes.append(("POST", new["account_id"], body))
        return new


def as_row(p):
    return {"kind": p.kind, "account_id": p.account_id, "changes": p.changes, "new_account": p.new_account,
            "note": p.note, "snapshot": p.snapshot}


def test_apply_chow_leaves_old_account_untouched(data, result):
    crm = FakeCRM(data[1])
    (p,) = by_account(result[0], "001U6RW32TY0WSXZZB")
    out = apply(crm, as_row(p))
    old = crm.accts["001U6RW32TY0WSXZZB"]
    assert old["parent_id"] != BELLHAVEN and old["chow_current_account"] == out["created"]
    assert [w[0] for w in crm.writes] == ["POST", "PATCH"] and crm.writes[1][2] == {"chow_current_account": out["created"]}


def test_apply_refuses_reparent_if_ar_appeared(data, result):
    crm = FakeCRM(data[1])
    (p,) = by_account(result[0], "001LGFPBJY4N9MB6KL")
    crm.accts["001LGFPBJY4N9MB6KL"]["outstanding_ar"] = 500
    with pytest.raises(Stale):
        apply(crm, as_row(p))
    assert not crm.writes


def test_undo_restores_exact_previous_values(data, result):
    from bellhaven_sync.apply import undo
    crm = FakeCRM(data[1])
    before = copy.deepcopy(crm.accts["001LGFPBJY4N9MB6KL"])
    (p,) = by_account(result[0], "001LGFPBJY4N9MB6KL")
    row = as_row(p)
    row["result"] = apply(crm, row)
    assert crm.accts["001LGFPBJY4N9MB6KL"]["parent_id"] == BELLHAVEN
    assert undo(crm, row) == "pending"
    assert crm.accts["001LGFPBJY4N9MB6KL"] == before


def test_undo_create_deactivates_and_allows_recreate(data, result):
    from bellhaven_sync.apply import undo
    crm = FakeCRM(data[1])
    (p,) = [x for x in result[0] if x.slug == "bellhaven-of-batavia"]
    row = as_row(p)
    row["result"] = apply(crm, row)
    new_id = row["result"]["created"]
    assert undo(crm, row) == "rejected"
    assert crm.accts[new_id]["status"] == "Inactive"
    assert apply(crm, row)["created"] != new_id  # undone account is not reused
