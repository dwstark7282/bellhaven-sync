"""Local review app. Nothing reaches the CRM except through POST /decide with approve.

    python -m bellhaven_sync.app      # http://127.0.0.1:5050
"""
from __future__ import annotations

import os

from flask import Flask, redirect, render_template_string, request, url_for

from . import pipeline, store
from .apply import Stale, apply
from .crm import CRM

app = Flask(__name__)
REVIEWER = os.environ.get("BH_REVIEWER", os.environ.get("USER", "reviewer"))
TABS = {"pending": ("pending",), "applied": ("applied",), "rejected": ("rejected",),
        "failed": ("failed", "superseded")}
KIND_LABEL = {"update": "Fix / re-parent", "chow": "Change of ownership", "create": "New account",
              "duplicate": "Duplicate", "sold": "Sold", "not_listed": "Not on website"}
SHOWN_FIELDS = ["name", "parent_name", "billing_street", "billing_city", "billing_state", "billing_zip",
                "phone", "care_type", "status", "lifetime_revenue", "outstanding_ar",
                "chow_current_account", "duplicate_of_account", "note"]


def db():
    return store.connect()


@app.get("/")
def index():
    tab = request.args.get("tab", "pending")
    conn = db()
    rows = store.list_by_status(conn, *TABS.get(tab, ("pending",))) if tab != "matched" else []
    counts = {t: len(store.list_by_status(conn, *s)) for t, s in TABS.items()}
    run = store.last_run(conn)
    matched = run["summary"]["matched"] if run else []
    counts["matched"] = len(matched)
    return render_template_string(PAGE, tab=tab, rows=rows, counts=counts, run=run, matched=matched,
                                  kind_label=KIND_LABEL, fields=SHOWN_FIELDS, msg=request.args.get("msg"))


@app.post("/decide/<pid>")
def decide(pid):
    conn, action = db(), request.form["action"]
    p = store.get(conn, pid)
    if not p or p["status"] not in ("pending", "failed"):
        return redirect(url_for("index", msg="Already decided."))
    if action == "reject":
        store.decide(conn, pid, "rejected", REVIEWER, {"comment": request.form.get("comment", "")})
        return redirect(url_for("index", msg=f"Rejected: {p['title']}"))
    try:
        result = apply(CRM(), p)
        store.decide(conn, pid, "applied", REVIEWER, result)
        msg = f"Applied: {p['title']}"
    except Stale as e:
        # Not a decision: the next run re-proposes it against fresh data.
        store.decide(conn, pid, "superseded", REVIEWER, {"error": str(e)})
        msg = f"Not applied (stale): {e}"
    except Exception as e:  # noqa: BLE001 -- surface any API failure to the reviewer, keep retryable
        store.decide(conn, pid, "failed", REVIEWER, {"error": str(e)})
        msg = f"Failed: {e}"
    return redirect(url_for("index", tab=request.args.get("tab", "pending"), msg=msg))


@app.post("/run")
def run_now():
    s = pipeline.run(db())
    q = s["queue"]
    return redirect(url_for("index", msg=f"Pipeline ran: {s['locations']} locations, {q['new']} new, "
                                         f"{q['already_decided']} already decided, {q['superseded']} superseded"))


PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Bellhaven Sync Review</title>
<style>
body{font:14px/1.45 -apple-system,system-ui,sans-serif;margin:0;background:#f6f7f9;color:#1d2330}
header{background:#1f3b34;color:#fff;padding:14px 24px;display:flex;justify-content:space-between;align-items:center}
header form button{background:#c9a227;border:0;padding:8px 14px;border-radius:4px;font-weight:600;cursor:pointer}
nav{padding:10px 24px;background:#fff;border-bottom:1px solid #dde1e7}
nav a{margin-right:18px;text-decoration:none;color:#445}nav a.on{color:#1f3b34;font-weight:700}
main{max-width:1150px;margin:0 auto;padding:18px 24px}
.msg{background:#fff8e1;border-left:4px solid #c9a227;padding:10px 14px;margin-bottom:14px}
.card{background:#fff;border:1px solid #dde1e7;border-radius:6px;padding:16px 18px;margin-bottom:14px}
.kind{display:inline-block;font-size:11px;text-transform:uppercase;letter-spacing:.05em;padding:2px 8px;border-radius:3px;background:#e7eef9;color:#244}
.kind.chow,.kind.sold{background:#fde8e8;color:#842}.kind.create{background:#e6f5ea;color:#264}
.kind.duplicate{background:#eee;color:#444}.kind.not_listed{background:#fff2d6;color:#753}
h3{margin:6px 0 8px;font-size:16px}.reason{margin:6px 0 10px}
table{border-collapse:collapse;width:100%;font-size:13px}td,th{border-bottom:1px solid #eef0f3;padding:4px 8px;text-align:left;vertical-align:top}
th{color:#667;font-weight:500;width:170px}.chg{background:#fff4c2}.new{color:#175;font-weight:600}.old{color:#a33;text-decoration:line-through}
.sig span{display:inline-block;margin:2px 6px 2px 0;padding:1px 7px;border-radius:10px;font-size:12px}
.y{background:#e6f5ea;color:#264}.n{background:#f3f3f3;color:#888}
details{margin-top:8px}summary{cursor:pointer;color:#556}
.actions{display:flex;gap:8px;margin-top:12px;align-items:center}
.actions button{padding:7px 16px;border-radius:4px;border:1px solid #bbb;cursor:pointer;font-weight:600}
.approve{background:#1f7a4d;color:#fff;border-color:#1f7a4d!important}.reject{background:#fff;color:#a33}
.actions input{flex:1;padding:6px;border:1px solid #ccd;border-radius:4px}
.meta{color:#778;font-size:12px}code{font-size:12px}
</style></head><body>
<header><div><b>Bellhaven Sync</b> · review queue
{% if run %}<span class="meta" style="color:#cdd">&nbsp; last run {{run.finished}} · {{run.summary.locations}} site locations · {{run.summary.accounts}} CRM accounts</span>{% endif %}</div>
<form method="post" action="/run"><button>Run pipeline now</button></form></header>
<nav>{% for t in ["pending","applied","rejected","failed","matched"] %}<a href="/?tab={{t}}" class="{{'on' if t==tab}}">{{t|capitalize}} ({{counts[t]}})</a>{% endfor %}</nav>
<main>
{% if msg %}<div class="msg">{{msg}}</div>{% endif %}

{% if tab == "matched" %}
<div class="card"><h3>Confident matches: no change needed</h3><table>
<tr><th>Website</th><th>CRM account</th><th>Signals</th></tr>
{% for m in matched %}<tr><td>{{m.name}}</td><td><code>{{m.account_id}}</code></td>
<td class="sig">{% for k,v in m.signals.items() %}<span class="{{'y' if v and v!=0 else 'n'}}">{{k}}{% if v is number and v is not boolean %} {{v}}{% endif %}</span>{% endfor %}</td></tr>{% endfor %}
</table></div>
{% endif %}

{% for p in rows %}
{% set crm = p.evidence.crm or {} %}{% set site = p.evidence.website %}
<div class="card">
<span class="kind {{p.kind}}">{{kind_label[p.kind]}}</span>
<span class="meta">&nbsp;{{p.id}} · first seen {{p.first_seen}}{% if p.decided_at %} · {{p.status}} {{p.decided_at}} by {{p.decided_by}}{% endif %}</span>
<h3>{{p.title}}</h3>
<div class="reason">{{p.evidence.reason}}</div>

{% if p.kind == "create" or p.kind == "chow" %}
<b>New account to create</b>
<table>{% for k,v in p.new_account.items() %}<tr><th>{{k}}</th><td class="new">{{v}}</td></tr>{% endfor %}</table>
{% endif %}
{% if p.kind == "create" %}
<b style="display:block;margin-top:14px">Closest existing CRM accounts in {{site.city}}, {{site.state}}</b>
{% if p.evidence.same_city_accounts %}
<div class="meta">Checked and ruled out: street and phone don't match the website. Reject this card if one of these is actually the same building.</div>
<table><tr><th>Account</th><th>Owner</th><th>Street</th><th>Phone</th><th>Name similarity</th></tr>
<tr style="color:#667"><td><i>Website listing</i></td><td>Bellhaven</td><td>{{site.street}}</td><td>{{site.phone}}</td><td></td></tr>
{% for o in p.evidence.same_city_accounts %}<tr><td>{{o.name}}<br><code>{{o.account_id}}</code></td><td>{{o.parent}}</td><td>{{o.street}}</td><td>{{o.phone}}</td><td>{{o.name_similarity}}</td></tr>{% endfor %}</table>
{% else %}<div class="meta">None: no CRM account in this city at all.</div>{% endif %}
{% endif %}

{% if p.kind != "create" %}
<table style="margin-top:8px"><tr><th></th><th>Website{% if site %} · <a href="{{site.url}}" target="_blank">listing</a>{% endif %}</th><th>CRM {% if crm.account_id %}<code>{{crm.account_id}}</code>{% endif %}</th><th>Proposed change</th></tr>
{% set sitemap = {"name":site.name,"billing_street":site.street,"billing_city":site.city,"billing_state":site.state,"billing_zip":site.zip,"phone":site.phone,"care_type":(site.care_offerings|join(", ")) if site else None} if site else {} %}
{% for f in fields %}{% set key = "parent_id" if f=="parent_name" else f %}{% set changed = key in p.changes %}
{% if crm.get(f) not in (None,"") or sitemap.get(f) or changed %}
<tr class="{{'chg' if changed}}"><th>{{f}}</th><td>{{sitemap.get(f) or ""}}</td><td {% if changed %}class="old"{% endif %}>{{crm.get(f)}}</td>
<td class="new">{% if changed %}{{"Bellhaven Senior Living" if f=="parent_name" else p.changes[key]}}{% endif %}</td></tr>{% endif %}
{% endfor %}
{% if p.note %}<tr class="chg"><th>note (appended)</th><td></td><td></td><td class="new">{{p.note}}</td></tr>{% endif %}
</table>
{% endif %}

{% if p.evidence.signals %}<div class="sig" style="margin-top:8px">{% for k,v in p.evidence.signals.items() %}<span class="{{'y' if v and v!=0 else 'n'}}">{{k}}{% if v is number and v is not boolean %} {{v}}{% endif %}</span>{% endfor %}
{% if p.evidence.contacts %}<span class="meta">contacts: {{p.evidence.contacts|join(", ")}}</span>{% endif %}</div>{% endif %}

{% if p.evidence.survivor %}<div class="meta" style="margin-top:6px">Survivor: <b>{{p.evidence.survivor.name}}</b> <code>{{p.evidence.survivor.account_id}}</code> under {{p.evidence.survivor.parent_name or "(no parent)"}}</div>{% endif %}

{% if p.evidence.candidates and p.evidence.candidates|length > 1 %}<details open><summary>{{p.evidence.candidates|length}} CRM accounts for this facility</summary><table>
<tr><th>Account</th><th>Parent</th><th>Street</th><th>Rev / AR</th><th>Signals</th></tr>
{% for c in p.evidence.candidates %}<tr><td>{{c.account.name}}<br><code>{{c.account.account_id}}</code></td><td>{{c.account.parent_name or "(none)"}}</td><td>{{c.account.billing_street}}</td>
<td>${{"{:,}".format(c.account.lifetime_revenue)}} / ${{"{:,}".format(c.account.outstanding_ar)}}</td>
<td class="sig">{% for k,v in c.signals.items() %}<span class="{{'y' if v and v!=0 else 'n'}}">{{k}}{% if v is number and v is not boolean %} {{v}}{% endif %}</span>{% endfor %}{% if c.contacts %}<br><span class="meta">{{c.contacts|join(", ")}}</span>{% endif %}</td></tr>{% endfor %}
</table></details>{% endif %}

{% if p.evidence.same_address_elsewhere %}<details open><summary>Same address under another parent</summary><table>
{% for o in p.evidence.same_address_elsewhere %}<tr><td>{{o.name}} <code>{{o.account_id}}</code></td><td>{{o.parent_name}}</td><td>{{o.billing_street}}, {{o.billing_city}}</td></tr>{% endfor %}</table></details>{% endif %}



{% if p.result %}<div class="meta" style="margin-top:8px">Result: <code>{{p.result}}</code></div>{% endif %}

{% if p.status in ("pending","failed") %}
<form class="actions" method="post" action="/decide/{{p.id}}?tab={{tab}}">
<button class="approve" name="action" value="approve">{{"Retry" if p.status=="failed" else "Approve & write to CRM"}}</button>
<button class="reject" name="action" value="reject">Reject</button>
<input name="comment" placeholder="reason (optional, saved with a rejection)">
</form>{% endif %}
</div>
{% else %}{% if tab != "matched" %}<div class="card">Nothing here.</div>{% endif %}{% endfor %}
</main></body></html>"""


if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", 5050)), debug=False)
