import json
import unittest
import urllib.error
import urllib.request

from app.server import make_server
from app.store import AuditStore

import threading


def unsat_payload(request_id):
    return {
        "request_id": request_id,
        "modulus": 100,
        "anchor": {"id": "A", "absolute": 95},
        "events": [{"id": "B", "counter": 4}],
        "constraints": [
            {"id": "c1", "source": "A", "target": "B", "lo": 8, "hi": 8},
        ],
    }


def exhausted_payload(request_id):
    return {
        "request_id": request_id,
        "modulus": 100,
        "anchor": {"id": "A", "absolute": 95},
        "events": [{"id": "B", "counter": 3}],
        "constraints": [
            {"id": "c1", "source": "A", "target": "B", "lo": 8, "hi": 8},
            {"id": "c2", "source": "B", "target": "A", "lo": -108, "hi": -108},
        ],
    }


class RepairApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = AuditStore()
        cls.server = make_server("127.0.0.1", 0, cls.store)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def request(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode() or "{}")

    def make_audit(self, payload):
        status, body = self.request("POST", "/audits", payload)
        self.assertEqual(status, 201, body)
        return body["audit_id"]

    def test_repair_cross_wrap_and_read_back(self):
        audit_id = self.make_audit(unsat_payload("api-fix-1"))
        status, body = self.request(
            "POST", f"/audits/{audit_id}/repairs",
            {"fix_id": "fix-cross", "K": 1})
        self.assertEqual(status, 201, body)
        self.assertFalse(body["replayed"])
        self.assertRegex(body["repair_id"], r"^FIX-\d{6}$")
        self.assertEqual(body["status"], "repaired")
        b = next(c for c in body["canonical_corrections"] if c["id"] == "B")
        self.assertEqual(b["tick_adjustment"], -1)
        self.assertEqual(b["corrected_counter"], 3)
        tl = {e["id"]: e for e in body["repaired_timeline"]}
        self.assertEqual(tl["B"]["absolute"], 103)
        delta, = body["constraint_deltas"]
        self.assertEqual(delta["absolute_delta"], 8)
        self.assertTrue(delta["satisfied"])

        status, fetched = self.request(
            "GET", f"/repairs/{body['repair_id']}")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["canonical_corrections"],
                         body["canonical_corrections"])
        self.assertEqual(fetched["frozen_source"], unsat_payload("api-fix-1"))

        status, listing = self.request(
            "GET", f"/audits/{audit_id}/repairs")
        self.assertEqual(status, 200)
        self.assertEqual(listing["repairs"], [body["repair_id"]])
        self.assertEqual(listing["count"], 1)

    def test_fix_id_replay_and_conflict_on_K(self):
        audit_id = self.make_audit(unsat_payload("api-fix-2"))
        _, first = self.request(
            "POST", f"/audits/{audit_id}/repairs",
            {"fix_id": "fix-replay", "K": 1})
        status, replay = self.request(
            "POST", f"/audits/{audit_id}/repairs",
            {"fix_id": "fix-replay", "K": 1})
        self.assertEqual(status, 200)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["repair_id"], first["repair_id"])

        status, conflict = self.request(
            "POST", f"/audits/{audit_id}/repairs",
            {"fix_id": "fix-replay", "K": 2})
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"], "fix_id_conflict")
        self.assertEqual(conflict["existing_repair_id"], first["repair_id"])
        self.assertEqual(conflict["bound_K"], 1)

    def test_fix_id_cannot_move_to_another_source(self):
        a1 = self.make_audit(unsat_payload("api-fix-3a"))
        a2 = self.make_audit(unsat_payload("api-fix-3b"))
        _, first = self.request(
            "POST", f"/audits/{a1}/repairs",
            {"fix_id": "fix-move", "K": 1})
        status, conflict = self.request(
            "POST", f"/audits/{a2}/repairs",
            {"fix_id": "fix-move", "K": 1})
        self.assertEqual(status, 409)
        self.assertEqual(conflict["bound_source_audit_id"], a1)
        # the second source audit was not rewritten
        status, record = self.request("GET", f"/audits/{a2}")
        self.assertEqual(status, 200)
        self.assertEqual(record["status"], "unsatisfiable")
        self.assertNotIn("repairs", record)

    def test_beyond_budget_record_is_idempotent(self):
        audit_id = self.make_audit(exhausted_payload("api-fix-4"))
        status, body = self.request(
            "POST", f"/audits/{audit_id}/repairs",
            {"fix_id": "fix-ex", "K": 1})
        self.assertEqual(status, 201)
        self.assertEqual(body["status"], "beyond_budget")
        self.assertIn("tree", body["search"])
        status, replay = self.request(
            "POST", f"/audits/{audit_id}/repairs",
            {"fix_id": "fix-ex", "K": 1})
        self.assertEqual(status, 200)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["repair_id"], body["repair_id"])

    def test_repair_on_satisfiable_audit_is_conflict(self):
        payload = unsat_payload("api-fix-5")
        payload["events"][0]["counter"] = 3  # now uniquely satisfiable
        audit_id = self.make_audit(payload)
        status, body = self.request(
            "POST", f"/audits/{audit_id}/repairs",
            {"fix_id": "fix-sat", "K": 1})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "source_not_unsatisfiable")

    def test_K_validation_and_unknown_routes(self):
        audit_id = self.make_audit(unsat_payload("api-fix-6"))
        status, body = self.request(
            "POST", f"/audits/{audit_id}/repairs",
            {"fix_id": "fix-bad", "K": 51})
        self.assertEqual(status, 400)
        self.assertTrue(any("[0, 50]" in p for p in body["problems"]))

        status, body = self.request(
            "POST", "/audits/AUD-999999/repairs", {"fix_id": "x", "K": 1})
        self.assertEqual(status, 404)
        status, body = self.request("GET", "/repairs/FIX-999999")
        self.assertEqual(status, 404)

    def test_global_optimum_tie_adjudicated_by_identifier_order(self):
        payload = {
            "request_id": "api-fix-tie",
            "modulus": 100,
            "anchor": {"id": "A", "absolute": 0},
            "events": [
                {"id": "B", "counter": 16},
                {"id": "C", "counter": 3},
            ],
            "constraints": [
                {"id": "c1", "source": "A", "target": "B",
                 "lo": -12, "hi": 25},
                {"id": "c2", "source": "B", "target": "C",
                 "lo": 88, "hi": 139},
            ],
        }
        audit_id = self.make_audit(payload)
        status, body = self.request(
            "POST", f"/audits/{audit_id}/repairs",
            {"fix_id": "fix-tie", "K": 1})
        self.assertEqual(status, 201, body)
        vec = {c["id"]: c["tick_adjustment"]
               for c in body["canonical_corrections"]}
        self.assertEqual(vec, {"B": -1, "C": 0})
        self.assertEqual(body["objective"]["changed_events"], 1)
        self.assertEqual(body["objective"]["abs_tick_sum"], 1)


if __name__ == "__main__":
    unittest.main()
