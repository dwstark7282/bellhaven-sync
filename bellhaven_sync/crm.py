"""Thin client for the CRM sandbox API."""
from __future__ import annotations

import requests

from . import config


class CRM:
    def __init__(self, token: str | None = None, session: requests.Session | None = None):
        token = token or config.API_TOKEN
        if not token:
            raise RuntimeError("Set BH_API_TOKEN")
        self.s = session or requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token}"

    def _req(self, method: str, path: str, **kw):
        resp = self.s.request(method, config.API_URL + path, timeout=30, **kw)
        if resp.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def _paged(self, path: str, **params) -> list[dict]:
        rows, page = [], 1
        while True:
            body = self._req("GET", path, params={**params, "page": page, "page_size": 100})
            rows += body["data"]
            if not body["data"] or len(rows) >= body["total"]:
                return rows
            page += 1

    def accounts(self, **filters) -> list[dict]:
        return self._paged("/accounts", **filters)

    def contacts(self) -> list[dict]:
        return self._paged("/contacts")

    def account(self, account_id: str) -> dict:
        return self._req("GET", f"/accounts/{account_id}")

    def update_account(self, account_id: str, fields: dict) -> dict:
        return self._req("PATCH", f"/accounts/{account_id}", json=fields)

    def create_account(self, fields: dict) -> dict:
        return self._req("POST", "/accounts", json=fields)
