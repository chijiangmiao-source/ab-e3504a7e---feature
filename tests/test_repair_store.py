import unittest

from app.solver import InputError
from app.store import (
    AuditStore,
    ConflictError,
    RepairConflictError,
    SourceNotRepairable,
)


def unsat_payload(request_id="u1"):
    # wrap-crossing conflict: B counter 4 must read 3 under the [8,8] window
    return {
        "request_id": request_id,
        "modulus": 100,
        "anchor": {"id": "A", "absolute": 95},
        "events": [{"id": "B", "counter": 4}],
        "constraints": [
            {"id": "c1", "source": "A", "target": "B", "lo": 8, "hi": 8},
        ],
    }


def exhausted_payload(request_id="u2"):
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


def unique_payload(request_id="ok1"):
    return {
        "request_id": request_id,
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
        self.record, _ = self.store.create(unsat_payload())
        self.source_id = self.record["audit_id"]

    def test_repair_assigns_sequential_ids_and_freezes_input(self):
        repair, created = self.store.create_repair(
            self.source_id, {"fix_id": "fix-1", "K": 1})
        self.assertTrue(created)
        self.assertEqual(repair["repair_id"], "FIX-000001")
        self.assertEqual(repair["fix_id"], "fix-1")
        self.assertEqual(repair["source_audit_id"], self.source_id)
        self.assertEqual(repair["K"], 1)
        self.assertEqual(repair["frozen_source"], unsat_payload())
        self.assertEqual(repair["frozen_modulus"], 100)
        self.assertEqual(repair["status"], "repaired")
        self.assertEqual(self.store.repair_count(), 1)

    def test_retransmitted_fix_id_replays_original_repair(self):
        first, c1 = self.store.create_repair(
            self.source_id, {"fix_id": "fix-1", "K": 1})
        second, c2 = self.store.create_repair(
            self.source_id, {"fix_id": "fix-1", "K": 1})
        self.assertTrue(c1)
        self.assertFalse(c2)
        self.assertEqual(second["repair_id"], first["repair_id"])
        self.assertEqual(self.store.repair_count(), 1)
        self.assertEqual(
            self.store.get_repair(first["repair_id"])["repair_id"],
            first["repair_id"])

    def test_same_fix_id_other_K_is_rejected(self):
        first, _ = self.store.create_repair(
            self.source_id, {"fix_id": "fix-1", "K": 1})
        with self.assertRaises(RepairConflictError) as ctx:
            self.store.create_repair(
                self.source_id, {"fix_id": "fix-1", "K": 2})
        self.assertEqual(ctx.exception.repair_id, first["repair_id"])
        self.assertEqual(ctx.exception.K, 1)
        self.assertEqual(self.store.repair_count(), 1)

    def test_same_fix_id_other_source_is_rejected(self):
        other, _ = self.store.create(unsat_payload("u1-other"))
        first, _ = self.store.create_repair(
            self.source_id, {"fix_id": "fix-1", "K": 1})
        with self.assertRaises(RepairConflictError) as ctx:
            self.store.create_repair(
                other["audit_id"], {"fix_id": "fix-1", "K": 1})
        self.assertEqual(ctx.exception.source_audit_id, self.source_id)
        # source audit untouched
        self.assertEqual(self.store.get(other["audit_id"])["status"],
                         "unsatisfiable")
        self.assertEqual(self.store.repair_count(), 1)

    def test_distinct_fix_ids_each_get_own_record(self):
        r1, _ = self.store.create_repair(
            self.source_id, {"fix_id": "fix-1", "K": 1})
        r2, _ = self.store.create_repair(
            self.source_id, {"fix_id": "fix-2", "K": 2})
        self.assertNotEqual(r1["repair_id"], r2["repair_id"])
        self.assertEqual(self.store.repair_count(), 2)
        self.assertEqual(
            self.store.repair_ids_for(self.source_id),
            [r1["repair_id"], r2["repair_id"]])

    def test_repair_against_satisfiable_audit_rejected(self):
        ok, _ = self.store.create(unique_payload())
        with self.assertRaises(SourceNotRepairable) as ctx:
            self.store.create_repair(
                ok["audit_id"], {"fix_id": "fix-x", "K": 1})
        self.assertEqual(ctx.exception.status, "unique")
        self.assertEqual(self.store.repair_count(), 0)

    def test_beyond_budget_still_creates_an_idempotent_record(self):
        ex, _ = self.store.create(exhausted_payload())
        repair, created = self.store.create_repair(
            ex["audit_id"], {"fix_id": "fix-ex", "K": 1})
        self.assertTrue(created)
        self.assertEqual(repair["status"], "beyond_budget")
        replay, again = self.store.create_repair(
            ex["audit_id"], {"fix_id": "fix-ex", "K": 1})
        self.assertFalse(again)
        self.assertEqual(replay["repair_id"], repair["repair_id"])

    def test_source_audit_is_never_rewritten(self):
        before = self.store.get(self.source_id)
        self.store.create_repair(
            self.source_id, {"fix_id": "fix-1", "K": 1})
        after = self.store.get(self.source_id)
        self.assertEqual(before, after)
        self.assertNotIn("repairs", after)

    def test_unknown_source_raises_keyerror(self):
        with self.assertRaises(KeyError):
            self.store.create_repair("AUD-999999", {"fix_id": "f", "K": 1})

    def test_existing_audit_semantics_still_work(self):
        # original idempotent creation/replay/conflict behaviour survives
        first, created = self.store.create(unique_payload("orig"))
        self.assertTrue(created)
        second, replay = self.store.create(unique_payload("orig"))
        self.assertFalse(replay)
        self.assertEqual(second["audit_id"], first["audit_id"])
        changed = unique_payload("orig")
        changed["constraints"][0]["hi"] = 9
        with self.assertRaises(ConflictError):
            self.store.create(changed)


if __name__ == "__main__":
    unittest.main()
