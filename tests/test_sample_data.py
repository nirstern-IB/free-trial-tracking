import importlib
import os
import unittest
from datetime import date, timedelta
from unittest.mock import patch


with patch.dict(os.environ, {}, clear=True):
    dashboard = importlib.import_module("app")


class SampleDataTests(unittest.TestCase):
    """Runs the dashboard's real SQL against the bundled sample data."""

    def setUp(self):
        patch.object(dashboard, "SAMPLE_MODE", True).start()
        patch.object(
            dashboard.snowflake.connector,
            "connect",
            side_effect=AssertionError("Sample mode must not connect to Snowflake"),
        ).start()
        self.addCleanup(patch.stopall)
        self.client = dashboard.app.test_client()

    def grid(self, **params):
        response = self.client.get("/api/grid", query_string=params)
        self.assertEqual(response.status_code, 200, response.json)
        return response.json["rows"]

    def test_dashboard_opens_without_login_and_shows_badge(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Sample data", response.data)
        self.assertNotIn(b'href="/logout"', response.data)

    def test_dashboard_defaults_to_last_30_days(self):
        response = self.client.get("/")
        expected_start = (date.today() - timedelta(days=30)).isoformat()
        self.assertIn(f'id="start-date" value="{expected_start}"'.encode(), response.data)
        self.assertIn(f'id="end-date" value="{date.today().isoformat()}"'.encode(), response.data)
        self.assertIn(b'<button onclick="setRange(30)" class="active">30d</button>', response.data)

        with patch.object(dashboard, "fetch_dashboard_data", return_value={}) as fetch_data:
            self.client.get("/api/data")
        self.assertEqual(fetch_data.call_args.args[1:3], (expected_start, date.today().isoformat()))

    def test_summary_counts_the_100_free_trial_users(self):
        response = self.client.get("/api/data", query_string={"start": "2024-01-01"})
        self.assertEqual(response.status_code, 200, response.json)
        summary = response.json["summary"]
        self.assertEqual(summary["total_agents"], 100)
        self.assertGreater(summary["total_builds"], 1000)
        self.assertGreater(summary["total_distribution"], 0)
        self.assertGreater(summary["total_cache"], 0)

    def test_sample_project_and_paid_license_builds_are_excluded(self):
        rows = self.grid(start="2024-01-01")
        self.assertFalse(any("sample project" in r["build_title"] for r in rows))
        self.assertEqual(len({r["license_key"] for r in rows}), 100)

    def test_filters_narrow_the_results(self):
        rows = self.grid()
        self.assertLess(len(self.grid(hide_unreal="1")), len(rows))
        license_key = rows[0]["license_key"]
        filtered = self.grid(filter_type="license_key", filter_value=license_key)
        self.assertTrue(filtered)
        self.assertTrue(all(r["license_key"] == license_key for r in filtered))
        email = next(r["email"] for r in rows if r["email"] != "N/A")
        self.assertTrue(self.grid(filter_type="email", filter_value=email))

    def test_build_drilldowns_return_data(self):
        rows = self.grid()
        cached = next(r for r in rows if r["with_cache"] == "yes")
        ids = {"build_id": cached["build_id"], "coordinator_id": cached["coordinator_id"]}
        build = self.client.get("/api/build", query_string=ids).json
        self.assertEqual(len(build["rows"]), 1)
        cache = self.client.get("/api/build/cache", query_string=ids).json
        self.assertEqual(len(cache["rows"]), 1)

    def test_export_contains_the_builds(self):
        response = self.client.get("/api/export")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers["Content-Disposition"])


if __name__ == "__main__":
    unittest.main()
