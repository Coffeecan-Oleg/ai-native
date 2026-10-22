import hashlib
import json
from pathlib import Path
import unittest

from gateway_mcp.backends import call_backend
from gateway_mcp.backends.common import BackendRouteError
from gateway_mcp.backends.platforma_demo_store import DATA_FILE

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = json.loads((ROOT / "gateway-platforma-tools.json").read_text(encoding="utf-8"))
ROUTES = {route["name"]: route for route in REGISTRY["tools"]}


def run_read(coroutine):
    # A bundled fixture read must complete without yielding to network/event-loop I/O.
    try:
        coroutine.send(None)
    except StopIteration as completed:
        return completed.value
    finally:
        coroutine.close()
    raise AssertionError("The fictional backend unexpectedly requested asynchronous I/O")


def call(name, **arguments):
    return run_read(call_backend(ROUTES[name], arguments))["data"]


class PlatformaDemoTests(unittest.TestCase):
    def test_context_is_fictional_and_has_no_local_identity(self):
        context = call("platforma.context.get")
        self.assertTrue(context["synthetic"])
        self.assertTrue(context["read_only"])
        self.assertEqual(context["visible_services"], 85)
        self.assertEqual(context["visible_requests"], 9144)
        self.assertEqual(context["snapshot_as_of"], "2026-10-15")
        self.assertNotIn("profile", context)

    def test_service_card_and_related_requests_keep_source(self):
        service = call("platforma.services.list", limit=1)["records"][0]
        card = call("platforma.services.get", service_id=service["id"])["record"]
        self.assertEqual(card, service)
        requests = call("platforma.requests.list", service_id=service["id"], period="all")
        self.assertGreater(requests["total"], 0)
        for request in requests["records"]:
            self.assertEqual(request["service_id"], service["id"])
            self.assertTrue(request["source"].startswith("platforma-training:synthetic.json#requests/"))

    def test_request_card_does_not_mutate_cached_data(self):
        row = call("platforma.requests.list", limit=1)["records"][0]
        card = call("platforma.requests.get", request_id=row["id"])["record"]
        self.assertEqual(card["id"], row["id"])
        card["title"] = "modified caller copy"
        self.assertNotEqual(call("platforma.requests.get", request_id=row["id"])["record"]["title"], card["title"])

    def test_full_pagination_has_no_duplicates_or_missing_requests(self):
        identifiers = []
        offset = 0
        while True:
            page = call("platforma.requests.list", period="all", limit=100, offset=offset, sort="id")
            identifiers.extend(row["id"] for row in page["records"])
            if not page["has_more"]:
                break
            self.assertGreater(page["next_offset"], offset)
            offset = page["next_offset"]
        self.assertEqual(len(identifiers), 9144)
        self.assertEqual(len(set(identifiers)), 9144)

    def test_overdue_is_relative_to_snapshot(self):
        page = call("platforma.requests.list", period="all", overdue_only=True, limit=100)
        self.assertGreater(page["total"], 0)
        for row in page["records"]:
            self.assertEqual(row["status"], "active")
            self.assertIsNone(row["accepted_at"])
            self.assertLess(row["due_at"], page["snapshot_as_of"])
            self.assertTrue(row["is_overdue"])

    def test_search_and_unit_filter_are_applied(self):
        self.assertEqual(call("platforma.services.list", query="")["total"], 85)
        sample = call("platforma.services.list", limit=1)["records"][0]
        page = call("platforma.services.list", query=sample["name"], unit_id=sample["unit_id"])
        self.assertIn(sample["id"], [row["id"] for row in page["records"]])
        self.assertTrue(all(row["unit_id"] == sample["unit_id"] for row in page["records"]))

    def test_bad_filters_and_bounds_are_rejected(self):
        invalid = [{"limit": 0}, {"limit": 101}, {"limit": True}, {"offset": -1},
                   {"status": "invented"}, {"due_from": "2026-02-30"}, {"overdue_only": "yes"},
                   {"unexpected": "argument"}, {"sort": "invented"}]
        for arguments in invalid:
            with self.subTest(arguments=arguments), self.assertRaises(BackendRouteError):
                call("platforma.requests.list", **arguments)

    def test_priority_filter_uses_the_values_in_the_snapshot(self):
        for priority in ("Обычный", "Высокий", "Критичный"):
            page = call("platforma.requests.list", priority=priority, period="all")
            self.assertGreater(page["total"], 0)
            self.assertTrue(all(row["priority"] == priority for row in page["records"]))

    def test_missing_or_path_like_identifier_does_not_read_files(self):
        for identifier in (None, "../../.env", "unknown-request"):
            with self.subTest(identifier=identifier), self.assertRaises(BackendRouteError):
                call("platforma.requests.get", request_id=identifier)
        with self.assertRaises(BackendRouteError):
            call("platforma.services.get")

    def test_five_read_routes_and_two_bounded_write_routes_exist(self):
        self.assertEqual(len(ROUTES), 7)
        reads = [route for route in ROUTES.values() if route["http_method"] == "GET"]
        self.assertEqual(len(reads), 5)
        self.assertTrue(all(route["scope"] == "platforma:read" for route in reads))
        forged = {**ROUTES["platforma.requests.get"], "name": "platforma.requests.delete"}
        with self.assertRaises(BackendRouteError):
            run_read(call_backend(forged, {"request_id": "any"}))
        for change in ({"http_method": "POST"}, {"scope": "platforma:write"}):
            with self.assertRaises(BackendRouteError):
                run_read(call_backend({**ROUTES["platforma.requests.get"], **change}, {"request_id": "any"}))

    def test_reads_preserve_fixture_bytes(self):
        before = hashlib.sha256(DATA_FILE.read_bytes()).hexdigest()
        call("platforma.services.list", query="сервис")
        call("platforma.requests.list", overdue_only=True)
        self.assertEqual(before, hashlib.sha256(DATA_FILE.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
