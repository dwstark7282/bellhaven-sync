"""All tunables in one place. Change behavior here, not in the matcher."""
from __future__ import annotations

import os

_ENV = os.path.join(os.path.dirname(__file__), "..", ".env")
if os.path.exists(_ENV):
    for line in open(_ENV):
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.strip().split("=", 1)
            os.environ.setdefault(k, v)

BASE_URL = os.environ.get("BH_BASE_URL", "https://analyst-assessment-production.up.railway.app")
API_URL = f"{BASE_URL}/api/v1"
API_TOKEN = os.environ.get("BH_API_TOKEN", "")
DB_PATH = os.environ.get("BH_DB_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "sync.db"))

# Parent accounts are resolved by name at runtime so ids never live in code.
TARGET_PARENT_NAME = "Bellhaven Senior Living (Parent Account)"

# Fields we keep in sync from the website onto a matched account.
# Remove a field here to stop proposing changes to it.
SYNC_FIELDS = ["name", "billing_street", "billing_city", "billing_state", "billing_zip", "phone"]

# Website care offering -> CRM care_type picklist.
CARE_TYPE_MAP = {
    "short-term rehabilitation & nursing": "Skilled Nursing",
    "skilled nursing": "Skilled Nursing",
    "assisted living": "Assisted Living",
    "memory support": "Memory Care",
    "memory care": "Memory Care",
    "independent living": "Independent Living",
}

NOTE_TAG = "[bellhaven-sync]"
