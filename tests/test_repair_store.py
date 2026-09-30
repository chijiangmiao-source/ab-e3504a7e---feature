import unittest

from app.repair import InputError
from app.store import (
    AuditNotRepairableError,
    AuditStore,
    RepairConflictError,
    UnknownAuditError,
)


UNSAT_PAYLOAD = {
    "request_id": "req-unsat",
    "modulus": 100,
    "anchor": {"id": "A", "absolute": 95},
    "events": [{"id": "B", "counter": 3}],
    "constraints": [
        {"id": "c1", "source": "A", "target": "B", "lo": 9, "hi": 9},
    ],
}

UNSAT_PAYLOAD_2 = {
    "request_id": "req-unsat-2",
    "modulus": 100,
    "anchor": {"id": "A", "absolute": 95},
    "events": [{"id": "B", "counter": 3}],
    "constraints": [
        {"id": "c1", "source": "A", "target": "B", "lo": 10, "hi": 10},
    ],
}

SAT_PAYLOAD = {
    "request_id": "req-sat",
    "modulus": 100,
    "anchor": {"id": "A", "absolute": 95},
    "events": [{"id": "B", "counter": 3}],
    "constraints": [
        {"id": "c1", "source": "A", "target": "B", "lo": 8, "hi": 8},
    ],
}


class RepairStoreTest(unittest.TestCase):
    def setUp(self):
        self.store = AuditStore()
        self.unsat, _ = self.store.create(UNSAT_PAYLOAD)
        self.unsat2, _ = self.store.create(UNSAT_PAYLOAD_2)
        self.sat, _ = self.store.create(SAT_PAYLOAD)

    def test_repair_created_on_unsatisfiable_audit(self):
        record, created = self.store.create_repair(
            self.unsat["audit_id"], {"repair_id": "fix-1", "k": 1})
        self.assertTrue(created)
        self.assertEqual(record["repair_number"], "FIX-000001")
        self.assertEqual(record["audit_id"], self.unsat["audit_id"])
        self.assertEqual(record["status"], "repaired")
        self.assertEqual(record["k"], 1)

    def test_source_audit_is_never_rewritten(self):
        before = dict(self.unsat)
        self.store.create_repair(
            self.unsat["audit_id"], {"repair_id": "fix-1", "k": 1})
        after = self.store.get(self.unsat["audit_id"])
        self.assertEqual(before, after)
        self.assertEqual(after["status"], "unsatisfiable")

    def test_replay_same_id_same_k_returns_original_number(self):
        first, created = self.store.create_repair(
            self.unsat["audit_id"], {"repair_id": "fix-1", "k": 1})
        self.assertTrue(created)
        second, replayed = self.store.create_repair(
            self.unsat["audit_id"], {"repair_id": "fix-1", "k": 1})
        self.assertFalse(replayed)
        self.assertEqual(second["repair_number"], first["repair_number"])
        self.assertEqual(len(self.store.repair_numbers(self.unsat["audit_id"])), 1)

    def test_same_id_different_k_rejected(self):
        first, _ = self.store.create_repair(
            self.unsat["audit_id"], {"repair_id": "fix-1", "k": 1})
        with self.assertRaises(RepairConflictError) as ctx:
            self.store.create_repair(
                self.unsat["audit_id"], {"repair_id": "fix-1", "k": 2})
        self.assertEqual(ctx.exception.repair_number, first["repair_number"])
        # rejected retry adds no record
        self.assertEqual(len(self.store.repair_numbers(self.unsat["audit_id"])), 1)

    def test_same_id_different_source_audit_rejected(self):
        first, _ = self.store.create_repair(
            self.unsat["audit_id"], {"repair_id": "fix-1", "k": 1})
        with self.assertRaises(RepairConflictError) as ctx:
            self.store.create_repair(
                self.unsat2["audit_id"], {"repair_id": "fix-1", "k": 1})
        self.assertEqual(ctx.exception.audit_id, self.unsat["audit_id"])
        self.assertEqual(
            self.store.repair_numbers(self.unsat2["audit_id"]), [])

    def test_different_repair_ids_both_allowed(self):
        r1, _ = self.store.create_repair(
            self.unsat["audit_id"], {"repair_id": "fix-a", "k": 1})
        r2, _ = self.store.create_repair(
            self.unsat["audit_id"], {"repair_id": "fix-b", "k": 2})
        self.assertNotEqual(r1["repair_number"], r2["repair_number"])
        self.assertEqual(
            self.store.repair_numbers(self.unsat["audit_id"]),
            [r1["repair_number"], r2["repair_number"]])

    def test_repair_on_non_unsatisfiable_audit_rejected(self):
        with self.assertRaises(AuditNotRepairableError) as ctx:
            self.store.create_repair(
                self.sat["audit_id"], {"repair_id": "fix-x", "k": 1})
        self.assertEqual(ctx.exception.status, "unique")

    def test_repair_on_unknown_audit(self):
        with self.assertRaises(UnknownAuditError):
            self.store.create_repair(
                "AUD-999999", {"repair_id": "fix-x", "k": 1})

    def test_invalid_repair_body_claims_no_id(self):
        with self.assertRaises(InputError):
            self.store.create_repair(
                self.unsat["audit_id"], {"repair_id": "fix-y", "k": 51})
        # the rejected marker can be reused afterwards
        record, created = self.store.create_repair(
            self.unsat["audit_id"], {"repair_id": "fix-y", "k": 1})
        self.assertTrue(created)
        self.assertEqual(record["repair_number"], "FIX-000001")

    def test_exhausted_repair_is_stored_and_replays(self):
        payload = {
            "request_id": "req-hard",
            "modulus": 100,
            "anchor": {"id": "A", "absolute": 95},
            "events": [{"id": "B", "counter": 3}],
            "constraints": [
                {"id": "c1", "source": "A", "target": "B", "lo": 8, "hi": 8},
                {"id": "c2", "source": "B", "target": "A",
                 "lo": -108, "hi": -108}],
        }
        hard, _ = self.store.create(payload)
        record, created = self.store.create_repair(
            hard["audit_id"], {"repair_id": "fix-hard", "k": 2})
        self.assertTrue(created)
        self.assertEqual(record["status"], "exhausted")
        replay, replayed = self.store.create_repair(
            hard["audit_id"], {"repair_id": "fix-hard", "k": 2})
        self.assertFalse(replayed)
        self.assertEqual(replay["repair_number"], record["repair_number"])
        self.assertIsNotNone(self.store.get_repair(record["repair_number"]))


if __name__ == "__main__":
    unittest.main()
