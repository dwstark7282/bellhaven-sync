# bellhaven-sync

Keeps the CRM's picture of which facilities Bellhaven Senior Living owns in line with Bellhaven's website. A daily job scrapes the site, matches each community to CRM accounts, and queues proposed changes with evidence. A reviewer approves or rejects each one in a local web app. **Only approval writes to the CRM.**

```
website ──scrape──┐
                  ├─ match (pure, tested) ─ proposals ─ SQLite queue ─ review app ─ approve ─ apply ─ CRM API
CRM API ──read────┘                                                              └ reject ─ recorded, never re-proposed
```

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env                        # set BH_API_TOKEN
.venv/bin/python -m bellhaven_sync.pipeline # queue proposals (read-only against the CRM)
.venv/bin/python -m bellhaven_sync.app      # review at http://127.0.0.1:5050
.venv/bin/python -m pytest -q               # matcher + SOP + idempotency tests on a frozen snapshot
```

Schedule: `deploy/crontab` runs `deploy/daily.sh` at 06:00 CT on weekdays. The pipeline and the app share the SQLite file, so they run on the same box.

## Layout

| File | Job |
|---|---|
| `config.py` | Everything tunable: synced fields, care-type mapping, target parent |
| `scrape.py` | Home page + paginated directory, then each detail page |
| `normalize.py` | Comparison-only normalization (streets, names, phones) |
| `match.py` | Matching and classification into proposals. No I/O |
| `store.py` | Review queue keyed by a content fingerprint |
| `apply.py` | Writes one approved proposal; re-checks live data first |
| `app.py` | Review UI |

## Matching approach

1. **Scrape everything the site links.** The directory lists 34 communities, but the home page says 35. Bellhaven Meadows of Findlay is linked only from the home page, so the scraper collects detail links from both.
2. **Find candidates** among all non-parent accounts, not just Bellhaven's. A candidate either has the same normalized street + city + state (the zip is ignored because it can be wrong), or is in the same city and has an independent second signal: phone, administrator on file as a contact, or an almost-identical name. This catches the PO-box Ashtabula record and keeps lookalikes out: Carlisle PA vs New Carlisle OH, Amberly Manor in Colorado, and Union Square Senior Living at a different street.
3. **Pick one survivor per facility.** Preference order: already under Bellhaven, active, has revenue, has contacts, administrator/phone match, name similarity. The other accounts become duplicates (`duplicate_of_account` + Inactive). A duplicate with open AR goes to Needs Review instead of being deactivated.
4. **Classify the survivor.** It's a confident match, a field fix (the website is authoritative for name, address, phone, care type), or a parent move.
5. **Parent moves follow the billing SOP.** Revenue > 0 **and** AR > 0 means a CHOW: create a new account under Bellhaven and set `chow_current_account` on the untouched old one (Tiffin, Marietta). Otherwise re-parent directly (Lima, Zanesville, Kettering, Findlay). `apply.py` re-checks the SOP against live data at write time, so AR that appears after review can't slip through.
6. **Bellhaven accounts missing from the site.** If the same address exists under another parent, the facility was sold. Sandusky has revenue + AR, so it gets a CHOW link to the existing Millstone account; no new account is needed. With no evidence (Coldwater, Alliance), set **Needs Review** with a note rather than guess whether it was sold or closed.

## Re-run safety

Each proposal's id is a hash of *what* it changes (kind, account, field values). It excludes evidence and notes. Decided ids are never queued again. Pending ones are refreshed in place. Pending items a later run no longer produces are marked superseded. Applied changes stop showing up naturally because the CRM now agrees with the site, and resolved accounts (`duplicate_of_account` / `chow_current_account` set) leave the matching pool. Before writing, `apply.py` re-reads the account and refuses if anything the decision depended on has changed. Creates check for an existing account first, so retrying a half-finished CHOW doesn't double-create.

## Judgment calls

- Website wins on phone numbers. Most diffs are phone-only; drop `phone` from `SYNC_FIELDS` to stop syncing it.
- Names compare exactly, not fuzzily. The site spelling is the brand ("Healthcare Centre" vs "Health Care Center" is a rename).
- Kettering had three accounts, none under Bellhaven and none with revenue. The survivor comes from the tie-break; the reviewer sees all three side by side.
- Bellhaven at Union Square is created new even though "Union Square Senior Living" (Juniper Point) is in the same city. The street differs, so it's a different facility. The create card lists same-city accounts so a reviewer can override.

## What I'd build next

- Store the website snapshot each day and diff it, so "location disappeared" is an event with a date.
- Slack digest of new proposals, with links into the app.
- Batch approve for low-risk kinds (phone-only fixes); keep CHOW and sold always one at a time.
- Auth on the review app and a real DB once more than one reviewer uses it.
- Run the same pipeline for every parent company with a website, driven by config.
