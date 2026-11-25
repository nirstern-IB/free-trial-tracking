import importlib
import io
import os
import unittest
from unittest.mock import Mock, patch

from openpyxl import load_workbook


# Import without using any ambient authentication settings. No network calls
# are allowed: an unexpected Snowflake connection fails the test immediately.
with patch.dict(os.environ, {}, clear=True):
    dashboard = importlib.import_module("app")


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.connect = patch.object(
            dashboard.snowflake.connector,
            "connect",
            side_effect=AssertionError("Offline tests must not connect to Snowflake"),
        ).start()
        self.addCleanup(patch.stopall)
        self.client = dashboard.app.test_client()

    def test_login_page_loads(self):
        response = self.client.get("/login")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"<form", response.data)

    def test_empty_login_is_rejected_without_connecting(self):
        response = self.client.post("/login", data={"username": "  "})
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Please enter your Snowflake username.", response.data)
        self.connect.assert_not_called()

    def test_dashboard_redirects_unauthenticated_users(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/login")

    def test_apis_reject_unauthenticated_requests(self):
        for path in ("/api/data", "/api/grid", "/api/export", "/api/build", "/api/build/cache"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json["error"], "session_expired")

    def test_missing_build_id_is_rejected_before_query(self):
        connection = Mock()
        with patch.object(dashboard, "get_user_connection", return_value=connection):
            for path in ("/api/build", "/api/build/cache"):
                with self.subTest(path=path):
                    response = self.client.get(path)
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.json["error"], "build_id required")
        connection.cursor.assert_not_called()

    def test_build_details_bind_identifiers_as_parameters(self):
        connection = Mock()
        cursor = connection.cursor.return_value
        cursor.fetchall.return_value = []
        cursor.description = [("BUILD_ID",)]
        with patch.object(dashboard, "get_user_connection", return_value=connection):
            response = self.client.get(
                "/api/build", query_string={"build_id": "sample'", "coordinator_id": "node"}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {"columns": ["BUILD_ID"], "rows": []})
        query, parameters = cursor.execute.call_args.args
        self.assertIn("BUILD_ID = %s", query)
        self.assertEqual(parameters, ("sample'", "node"))

    def test_export_produces_readable_excel_file(self):
        connection = Mock()
        connection.cursor.return_value.fetchall.return_value = []
        with patch.object(dashboard, "get_user_connection", return_value=connection):
            response = self.client.get("/api/export")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers["Content-Disposition"])
        workbook = load_workbook(io.BytesIO(response.data))
        self.addCleanup(workbook.close)
        self.assertEqual(workbook.sheetnames, ["Builds"])

    def test_filter_escapes_quotes(self):
        clause = dashboard.build_filter_clause("license_key", "sample'key")
        self.assertEqual(clause, "AND fba.LICENSE_ID = 'sample''key'")

    def test_unsupported_or_empty_filters_are_ignored(self):
        for kind, value in ((None, None), ("license_key", ""), ("unsupported", "sample")):
            with self.subTest(kind=kind, value=value):
                self.assertEqual(dashboard.build_filter_clause(kind, value), "")


if __name__ == "__main__":
    unittest.main()
