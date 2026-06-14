"""
routes/config_routes.py
────────────────────────
Token validation, config persistence, and Notion schema discovery.
"""

from __future__ import annotations

import re

import requests
from flask import Blueprint, jsonify, request

from ..config import (
    MASTER_DB_ID,
    PROJECTS_DB_ID,
    WORK_SESSIONS_DB_ID,
    load_config,
    save_config,
)
from ..notion_client import NotionClient

bp = Blueprint("config", __name__)


@bp.route("/api/config", methods=["GET"])
def api_get_config():
    return jsonify(load_config())


@bp.route("/api/config", methods=["POST"])
def api_save_config():
    save_config(request.json)
    return jsonify({"ok": True})


@bp.route("/api/test-token", methods=["POST"])
def api_test_token():
    token = request.json.get("token", "").strip()
    if not token:
        return jsonify({"error": "No token provided"}), 400
    try:
        client = NotionClient(token)
        data   = client.get_user_me()
        name   = data.get("name") or data.get("bot", {}).get("owner", {}).get("user", {}).get("name", "")
        return jsonify({"ok": True, "name": name})
    except requests.HTTPError as e:
        return jsonify({"error": f"Notion API {e.response.status_code}: {e.response.text}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/api/discover", methods=["POST"])
def api_discover():
    """Discover WBS databases accessible to the integration."""
    token = request.json.get("token", "").strip()
    if not token:
        return jsonify({"error": "No token provided"}), 400
    try:
        client = NotionClient(token)
        all_dbs = client.search_wbs_databases()
        # Skip global databases; everything else is a project WBS
        skip = {
            MASTER_DB_ID.replace("-", ""),
            PROJECTS_DB_ID.replace("-", ""),
            WORK_SESSIONS_DB_ID.replace("-", ""),
        }
        result = []
        for db in all_dbs:
            db_id = db["id"].replace("-", "")
            if db_id in skip:
                continue
            title = "".join(t["plain_text"] for t in db.get("title", []))
            if title and re.match(r"^[^a-zA-Z]*wbs", title.strip(), re.IGNORECASE):
                result.append({"id": db["id"], "title": title, "url": db.get("url", "")})
        result.sort(key=lambda x: x["title"])
        return jsonify({"databases": result})
    except requests.HTTPError as e:
        return jsonify({"error": f"Notion API {e.response.status_code}: {e.response.text}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/api/schema", methods=["POST"])
def api_schema():
    token = request.json.get("token", "").strip()
    db_id = request.json.get("db_id", "").strip()
    try:
        client = NotionClient(token)
        schema = client.get_db_schema(db_id)
        cols   = [{"name": k, "type": v["type"]}
                  for k, v in schema.get("properties", {}).items()]
        cols.sort(key=lambda x: x["name"])
        return jsonify({"columns": cols})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/api/projects", methods=["POST"])
def api_projects():
    token = request.json.get("token", "").strip()
    try:
        client   = NotionClient(token)
        pages    = client.query_db(PROJECTS_DB_ID)
        projects = []
        for p in pages:
            name = ""
            for v in p["properties"].values():
                if v["type"] == "title":
                    name = "".join(r["plain_text"] for r in v["title"])
                    break
            if name:
                projects.append({"id": p["id"], "name": name})
        projects.sort(key=lambda x: x["name"])
        return jsonify({"projects": projects})
    except Exception as e:
        return jsonify({"error": str(e)}), 400
