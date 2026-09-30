import json
import threading
import unittest
import urllib.error
import urllib.request

from app.server import make_server
from app.store import AuditStore

UNSAT_PAYLOAD = {
    "request_id": "api-unsat",
    "modulus": 100,
    "anchor": {"id": "A", "absolute": 95},
    "events": [{"id": "B", "counter": 3}],
    "constraints": [
        {"id": "c1", "source": "A", "target": "B", "lo": 9, "hi": 9},
    ],
}

UNIQUE_PAYLOAD = {
    "request_id": "api-unique",
    "modulus": 100,
    "anchor": {"id": "A", "absolute": 95},
    "events": [{"id": "B", "counter": 3}],
    "constraints": [
        {"id": "c1", "source": "A", "target": "B", "lo": 8, "hi": 8},
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

    def create_unsat(self, request_id="api-unsat"):
        payload = dict(UNSAT_PAYLOAD, request_id=request_id)
        status, record = self.request("POST", "/audits", payload)
        self.assertEqual(status, 201, record)
        self.assertEqual(record["status"], "unsatisfiable")
        return record

    def test_cross_wrap_repair_success(self):
        audit = self.create_unsat("api-repair-ok")
        status, body = self.request(
            "POST", f"/audits/{audit['audit_id']}/repairs",
            {"repair_id": "fix-ok", "k": 1})
        self.assertEqual(status, 201, body)
        self.assertFalse(body["replayed"])
        self.assertEqual(body["status"], "repaired")
        corrections = {c["id"]: c for c in body["conclusion"]["canonical_corrections"]}
        self.assertEqual(corrections["B"]["correction"], 1)
        self.assertEqual(corrections["B"]["corrected_counter"], 4)
        self.assertEqual(body["conclusion"]["changed_count"], 1)
        self.assertEqual(body["conclusion"]["abs_correction_sum"], 1)
        timeline = {t["id"]: t for t in body["conclusion"]["timeline"]}
        self.assertEqual(timeline["B"]["absolute"], 104)
        for d in body["conclusion"]["constraint_deltas"]:
            self.assertTrue(d["satisfied"])
        self.assertRegex(body["repair_number"], r"^FIX-\d{6}$")

    def test_repair_replay_returns_same_number(self):
        audit = self.create_unsat("api-repair-replay")
        path = f"/audits/{audit['audit_id']}/repairs"
        s1, first = self.request("POST", path, {"repair_id": "fix-r", "k": 1})
        self.assertEqual(s1, 201)
        s2, replay = self.request("POST", path, {"repair_id": "fix-r", "k": 1})
        self.assertEqual(s2, 200)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["repair_number"], first["repair_number"])
        self.assertEqual(replay["conclusion"], first["conclusion"])

    def test_repair_changed_k_is_conflict(self):
        audit = self.create_unsat("api-repair-k")
        path = f"/audits/{audit['audit_id']}/repairs"
        s1, first = self.request("POST", path, {"repair_id": "fix-k", "k": 1})
        self.assertEqual(s1, 201)
        s2, body = self.request("POST", path, {"repair_id": "fix-k", "k": 2})
        self.assertEqual(s2, 409)
        self.assertEqual(body["error"], "repair_id_conflict")
        self.assertEqual(body["existing_repair_number"],
                         first["repair_number"])
        # no extra repair record
        listed = self.store.repair_numbers(audit["audit_id"])
        self.assertEqual(listed, [first["repair_number"]])

    def test_repair_id_on_other_source_is_conflict(self):
        a1 = self.create_unsat("api-src-1")
        a2 = self.create_unsat("api-src-2")
        s1, first = self.request(
            "POST", f"/audits/{a1['audit_id']}/repairs",
            {"repair_id": "shared", "k": 1})
        self.assertEqual(s1, 201)
        s2, body = self.request(
            "POST", f"/audits/{a2['audit_id']}/repairs",
            {"repair_id": "shared", "k": 1})
        self.assertEqual(s2, 409)
        self.assertEqual(body["existing_audit_id"], a1["audit_id"])
        self.assertEqual(self.store.repair_numbers(a2["audit_id"]), [])

    def test_repair_on_satisfiable_audit_rejected(self):
        _, unique = self.request("POST", "/audits", UNIQUE_PAYLOAD)
        status, body = self.request(
            "POST", f"/audits/{unique['audit_id']}/repairs",
            {"repair_id": "fix-x", "k": 1})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "audit_not_unsatisfiable")
        self.assertEqual(body["audit_status"], "unique")

    def test_repair_unknown_audit_is_404(self):
        status, body = self.request(
            "POST", "/audits/AUD-999999/repairs",
            {"repair_id": "fix-x", "k": 1})
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "not_found")

    def test_repair_bad_k_is_400_and_source_untouched(self):
        audit = self.create_unsat("api-repair-bad")
        status, body = self.request(
            "POST", f"/audits/{audit['audit_id']}/repairs",
            {"repair_id": "fix-bad", "k": 51})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_repair")
        self.assertTrue(body["problems"])
        self.assertEqual(self.store.repair_numbers(audit["audit_id"]), [])
        # the frozen audit still reports unsatisfiable
        _, fetched = self.request("GET", f"/audits/{audit['audit_id']}")
        self.assertEqual(fetched["status"], "unsatisfiable")
        self.assertEqual(fetched["repairs"], [])

    def test_exhausted_repair_within_budget_is_201_and_readable(self):
        hard_payload = {
            "request_id": "api-hard",
            "modulus": 100,
            "anchor": {"id": "A", "absolute": 95},
            "events": [{"id": "B", "counter": 3}],
            "constraints": [
                {"id": "c1", "source": "A", "target": "B", "lo": 8, "hi": 8},
                {"id": "c2", "source": "B", "target": "A",
                 "lo": -108, "hi": -108}],
        }
        _, hard = self.request("POST", "/audits", hard_payload)
        status, body = self.request(
            "POST", f"/audits/{hard['audit_id']}/repairs",
            {"repair_id": "fix-hard", "k": 2})
        self.assertEqual(status, 201)
        self.assertEqual(body["status"], "exhausted")
        ex = body["conclusion"]["exhausted"]
        self.assertTrue(ex["complete_proof"])
        self.assertTrue(ex["sample_branches"])
        for sample in ex["sample_branches"]:
            chain = sample["conflict_chain"]
            self.assertLess(chain["total_weight"], 0)
            self.assertEqual(
                chain["total_weight"],
                sum(s["weight"] for s in chain["steps"]))
        status, fetched = self.request(
            "GET", f"/repairs/{body['repair_number']}")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["conclusion"], body["conclusion"])

    def test_unknown_repair_is_404(self):
        status, body = self.request("GET", "/repairs/FIX-999999")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "not_found")

    def test_audit_read_lists_repairs(self):
        audit = self.create_unsat("api-repair-list")
        _, r1 = self.request(
            "POST", f"/audits/{audit['audit_id']}/repairs",
            {"repair_id": "fix-l1", "k": 1})
        _, r2 = self.request(
            "POST", f"/audits/{audit['audit_id']}/repairs",
            {"repair_id": "fix-l2", "k": 2})
        _, fetched = self.request("GET", f"/audits/{audit['audit_id']}")
        self.assertEqual(fetched["repairs"],
                         [r1["repair_number"], r2["repair_number"]])


if __name__ == "__main__":
    unittest.main()
