import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from focal.app import create_app
from focal.sync_engine import regenerate_focus_cache


class FakeResponse:
    def __init__(self, payload=None, status_code=200, text=""):
        self._payload = payload or {}
        self.status_code = status_code
        self.text = text
        self.ok = status_code < 400

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}: {self.text}")


class FakeNotionClient:
    def __init__(self, token):
        self.token = token
        self.patches = []
        self.created_pages = []

    def patch_page(self, page_id, payload):
        self.patches.append((page_id, payload))
        return FakeResponse()

    def get_page(self, page_id):
        return FakeResponse({
            "properties": {
                "Task": {"relation": [{"id": "master-1"}]},
                "Project": {"relation": [{"id": "project-1"}]},
                "Session Name": {"title": [{"plain_text": "Draft paper"}]},
                "Work Type": {"select": {"name": "✍️ Writing"}},
            }
        })

    def create_page(self, parent, properties):
        created = {
            "id": f"new-ws-{len(self.created_pages) + 1}",
            "url": f"https://app.notion.com/{len(self.created_pages) + 1}",
            "parent": parent,
            "properties": properties,
        }
        self.created_pages.append(created)
        return created

    def query_db(self, db_id, filter_body=None):
        return []


class FocusWorkSessionFlowTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.client = self.app.test_client()

    def test_focus_tasks_buckets_and_filters_completed_entries(self):
        today = __import__("datetime").date.today()
        overdue = (today.replace(day=today.day) if today.day > 1 else today)
        overdue_s = (today.fromordinal(today.toordinal() - 1)).isoformat()
        today_s = today.isoformat()
        week_s = (today.fromordinal(today.toordinal() + 3)).isoformat()
        later_s = (today.fromordinal(today.toordinal() + 10)).isoformat()

        cache = {
            "generated_at": "2026-06-28T00:00:00Z",
            "task_count": 5,
            "tasks": [
                {"ws_id": "ws-overdue", "name": "Overdue", "planned_end": overdue_s, "priority": "High", "ws_status": "In Progress"},
                {"ws_id": "ws-today", "name": "Today", "planned_end": today_s, "priority": "Urgent", "ws_status": "In Progress"},
                {"ws_id": "ws-week", "name": "Week", "planned_end": week_s, "priority": "Normal", "ws_status": "In Progress"},
                {"ws_id": "ws-done", "name": "Done", "planned_end": today_s, "priority": "Low", "ws_status": "Completed"},
                {"ws_id": "ws-later", "name": "Later", "planned_end": later_s, "priority": "Low", "ws_status": "In Progress"},
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "focus-cache.json"
            cache_path.write_text(json.dumps(cache), encoding="utf-8")
            with patch("focal.routes.dashboard_routes.FOCUS_CACHE_FILE", str(cache_path)):
                with patch("focal.routes.dashboard_routes.load_config", return_value={"token": "saved-token"}):
                    response = self.client.post("/api/focus-tasks", json={})

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual([t["name"] for t in payload["overdue"]], ["Overdue"])
        self.assertEqual([t["name"] for t in payload["due_today"]], ["Today"])
        self.assertEqual([t["name"] for t in payload["this_week"]], ["Week"])

    def test_log_session_session_done_creates_continuation(self):
        fake_client = FakeNotionClient("token")
        mappings = {
            "wbs-1": {
                "ws_id": "ws-1",
                "name": "Draft paper",
                "planned_end": "2026-06-30",
                "priority": "High",
                "project_id": "project-1",
                "source_db_id": "source-1",
            }
        }

        with patch("focal.routes.task_routes.NotionClient", return_value=fake_client):
            with patch("focal.routes.task_routes._next_continuation_name", return_value="Draft paper-2"):
                with patch("focal.routes.task_routes.load_sessions_mappings", return_value=mappings):
                    with patch("focal.routes.task_routes.save_sessions_mappings"):
                        with patch("focal.routes.task_routes.regenerate_focus_cache"):
                            response = self.client.post("/api/log-session", json={
                                "token": "token",
                                "ws_id": "ws-1",
                                "project_id": "project-1",
                                "task_name": "Draft paper",
                                "session_start": "2026-06-28T10:00:00-04:00",
                                "session_status": "Session Done",
                            })

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["continuation_ws_name"], "Draft paper-2")
        self.assertEqual(len(fake_client.created_pages), 1)
        self.assertEqual(fake_client.created_pages[0]["properties"]["Status"]["select"]["name"], "In Progress")

        self.assertEqual(mappings["wbs-1"]["ws_id"], "new-ws-1")
        self.assertEqual(mappings["wbs-1"]["status"], "In Progress")

    def test_log_session_without_ws_id_creates_standalone_session(self):
        fake_client = FakeNotionClient("token")
        with patch("focal.routes.task_routes.NotionClient", return_value=fake_client):
            with patch("focal.routes.task_routes.regenerate_focus_cache"):
                response = self.client.post("/api/log-session", json={
                    "token": "token",
                    "project_id": "project-1",
                    "task_name": "Standalone session",
                    "session_start": "2026-06-28T11:00:00-04:00",
                })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(fake_client.created_pages), 1)
        created = fake_client.created_pages[0]["properties"]
        self.assertEqual(created["Session Name"]["title"][0]["text"]["content"], "Standalone session")
        self.assertEqual(created["Project"]["relation"][0]["id"], "project-1")

    def test_session_done_continuation_keeps_task_in_focus(self):
        # After Session Done + continuation creation, the mapping is re-pointed
        # to the new continuation WS.  Focus should still show the task.
        mappings = {
            "wbs-1": {
                "ws_id": "ws-new",
                "status": "In Progress",
                "name": "Draft paper",
                "planned_end": "2026-06-30",
                "priority": "High",
                "work_type": "✍️ Writing",
                "project_name": "Project Alpha",
                "source_db_id": "source-1",
            }
        }

        class CacheClient:
            def query_db(self, db_id):
                return [
                    {"id": "ws-closed", "properties": {"Status": {"type": "select", "select": {"name": "Session Done"}}}},
                    {"id": "ws-new",    "properties": {"Status": {"type": "select", "select": {"name": "In Progress"}}}},
                ]

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "focus-cache.json"
            with patch("focal.sync_engine.load_sessions_mappings", return_value=mappings):
                with patch("focal.sync_engine.FOCUS_CACHE_FILE", str(cache_path)):
                    regenerate_focus_cache(CacheClient())

            cache = json.loads(cache_path.read_text(encoding="utf-8"))

        self.assertEqual(cache["task_count"], 1)
        self.assertEqual(cache["tasks"][0]["ws_id"], "ws-new")


if __name__ == "__main__":
    unittest.main()
