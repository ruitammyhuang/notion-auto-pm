"""
notion_client.py
────────────────
NotionClient: thin OOP wrapper around the Notion REST API.
All HTTP calls live here; sync logic never imports `requests` directly.

Also exports module-level property-payload builders (p_title, p_text, …)
and the extract() helper — these are pure functions with no state.
"""

from __future__ import annotations

import requests
from typing import Any

from .config import NOTION_API, NOTION_VERSION


# ── Property value extraction ──────────────────────────────────────────────────
def extract(prop: dict) -> Any:
    """Pull a plain Python value out of a Notion property dict."""
    t = prop.get("type")
    if t == "title":
        return "".join(r["plain_text"] for r in prop.get("title", []))
    if t == "rich_text":
        return "".join(r["plain_text"] for r in prop.get("rich_text", []))
    if t in ("select", "status"):
        s = prop.get(t)
        return s["name"] if s else None
    if t == "multi_select":
        return ", ".join(o["name"] for o in prop.get("multi_select", []))
    if t == "date":
        d = prop.get("date")
        return {"start": d["start"], "end": d.get("end")} if d else None
    if t == "checkbox":
        return prop.get("checkbox")
    return None


# ── Property payload builders ──────────────────────────────────────────────────
def p_title(v: str) -> dict:
    return {"title": [{"text": {"content": v or ""}}]}


def p_text(v: str) -> dict:
    return {"rich_text": [{"text": {"content": str(v) if v else ""}}]}


def p_select(v: str) -> dict:
    return {"select": {"name": v} if v else None}


def p_date(v) -> dict:
    if not v:
        return {"date": None}
    if isinstance(v, dict):
        d = {"start": v["start"]}
        if v.get("end"):
            d["end"] = v["end"]
        return {"date": d}
    return {"date": {"start": str(v)}}


# ── NotionClient ───────────────────────────────────────────────────────────────
class NotionClient:
    """
    Wraps the Notion REST API for a single integration token.

    Every method that talks to Notion is here. Callers (sync_engine, tasks,
    routes) create one instance per request and pass it down — no global state.

    Usage:
        client = NotionClient(token)
        pages  = client.query_db(db_id)
        page   = client.create_page({"database_id": db_id}, props)
    """

    def __init__(self, token: str) -> None:
        self.token = token

    # ── Internal ───────────────────────────────────────────────────────────────
    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    # ── Read helpers ───────────────────────────────────────────────────────────
    def query_db(self, db_id: str, filter_body: dict | None = None) -> list:
        """Return all pages from a database, handling pagination automatically."""
        pages, cursor = [], None
        while True:
            body: dict = {"page_size": 100}
            if filter_body:
                body["filter"] = filter_body
            if cursor:
                body["start_cursor"] = cursor
            r = requests.post(
                f"{NOTION_API}/databases/{db_id}/query",
                headers=self._headers,
                json=body,
                timeout=20,
            )
            r.raise_for_status()
            data = r.json()
            pages.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return pages

    def search_wbs_databases(self) -> list:
        """Return all databases whose title starts with 'WBS' (case-insensitive)."""
        dbs, cursor = [], None
        while True:
            body: dict = {
                "query": "WBS",
                "filter": {"value": "database", "property": "object"},
                "page_size": 100,
            }
            if cursor:
                body["start_cursor"] = cursor
            r = requests.post(
                f"{NOTION_API}/search",
                headers=self._headers,
                json=body,
                timeout=20,
            )
            r.raise_for_status()
            data = r.json()
            dbs.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return dbs

    def get_db_schema(self, db_id: str) -> dict:
        r = requests.get(
            f"{NOTION_API}/databases/{db_id}",
            headers=self._headers,
            timeout=10,
        )
        r.raise_for_status()
        return r.json()

    def get_page(self, page_id: str) -> requests.Response:
        """Return the raw Response so callers can check status_code if needed."""
        return requests.get(
            f"{NOTION_API}/pages/{page_id}",
            headers=self._headers,
            timeout=10,
        )

    def get_user_me(self) -> dict:
        r = requests.get(f"{NOTION_API}/users/me", headers=self._headers, timeout=10)
        r.raise_for_status()
        return r.json()

    # ── Write helpers ──────────────────────────────────────────────────────────
    def create_page(self, parent: dict, properties: dict) -> dict:
        """Create a new Notion page/database entry. Returns the created page dict."""
        r = requests.post(
            f"{NOTION_API}/pages",
            headers=self._headers,
            json={"parent": parent, "properties": properties},
            timeout=20,
        )
        r.raise_for_status()
        return r.json()

    def patch_page(self, page_id: str, data: dict) -> requests.Response:
        """PATCH an existing page (update properties, archive, etc.)."""
        return requests.patch(
            f"{NOTION_API}/pages/{page_id}",
            headers=self._headers,
            json=data,
            timeout=15,
        )

    def get_block_children(self, block_id: str) -> list:
        """Return all child blocks of a page/block."""
        r = requests.get(
            f"{NOTION_API}/blocks/{block_id}/children",
            headers=self._headers,
            timeout=10,
        )
        r.raise_for_status()
        return r.json().get("results", [])

    def append_block_children(self, block_id: str, children: list) -> dict:
        r = requests.patch(
            f"{NOTION_API}/blocks/{block_id}/children",
            headers=self._headers,
            json={"children": children},
            timeout=15,
        )
        r.raise_for_status()
        return r.json()

    # ── Domain helpers ─────────────────────────────────────────────────────────
    def write_backlink(
        self,
        source_page_id: str,
        master_page_id: str,
        backlink_field: str,
    ) -> None:
        """Write Master WBS relation back to the source WBS page. Idempotent."""
        if not backlink_field:
            return
        try:
            self.patch_page(
                source_page_id,
                {"properties": {
                    backlink_field: {"relation": [{"id": master_page_id}]}
                }},
            )
        except Exception:
            pass  # non-fatal — mapping file still tracks the link
