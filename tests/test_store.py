import unittest

from app.solver import InputError
from app.store import AuditStore, ConflictError


def payload(request_id="req-1", hi=8):
    return {
        "request_id": request_id,
        "modulus": 100,
        "anchor": {"id": "A", "absolute": 95},
        "events": [{"id": "B", "counter": 3}],
        "constraints": [
            {"id": "c1", "source": "A", "target": "B", "lo": 8, "hi": hi},
        ],
    }


class AuditStoreTest(unittest.TestCase):
    def setUp(self):
        self.store = AuditStore()

    def test_create_assigns_sequential_ids(self):
        record, created = self.store.create(payload())
        self.assertTrue(created)
        self.assertEqual(record["audit_id"], "AUD-000001")
        record2, created2 = self.store.create(payload(request_id="req-2"))
        self.assertTrue(created2)
        self.assertEqual(record2["audit_id"], "AUD-000002")

    def test_replay_returns_original_audit(self):
        first, _ = self.store.create(payload())
        second, created = self.store.create(payload())
        self.assertFalse(created)
        self.assertEqual(second["audit_id"], first["audit_id"])
        self.assertEqual(self.store.count(), 1)

    def test_changed_payload_is_rejected_without_new_record(self):
        first, _ = self.store.create(payload())
        with self.assertRaises(ConflictError) as ctx:
            self.store.create(payload(hi=9))
        self.assertEqual(ctx.exception.audit_id, first["audit_id"])
        self.assertEqual(self.store.count(), 1)

    def test_changed_event_is_rejected(self):
        self.store.create(payload())
        changed = payload()
        changed["events"] = [{"id": "B", "counter": 4}]
        with self.assertRaises(ConflictError):
            self.store.create(changed)
        self.assertEqual(self.store.count(), 1)

    def test_reordered_payload_still_replays(self):
        first, _ = self.store.create({
            "request_id": "req-9",
            "modulus": 100,
            "anchor": {"id": "A", "absolute": 95},
            "events": [{"id": "B", "counter": 3}, {"id": "C", "counter": 50}],
            "constraints": [
                {"id": "c2", "source": "B", "target": "C", "lo": 47, "hi": 47},
                {"id": "c1", "source": "A", "target": "B", "lo": 8, "hi": 8},
            ],
        })
        reordered = {
            "request_id": "req-9",
            "modulus": 100,
            "anchor": {"id": "A", "absolute": 95},
            "events": [{"id": "C", "counter": 50}, {"id": "B", "counter": 3}],
            "constraints": [
                {"id": "c1", "source": "A", "target": "B", "lo": 8, "hi": 8},
                {"id": "c2", "source": "B", "target": "C", "lo": 47, "hi": 47},
            ],
        }
        second, created = self.store.create(reordered)
        self.assertFalse(created)
        self.assertEqual(second["audit_id"], first["audit_id"])

    def test_invalid_payload_claims_no_request_id(self):
        broken = payload()
        broken["events"] = []
        with self.assertRaises(InputError):
            self.store.create(broken)
        record, created = self.store.create(payload())
        self.assertTrue(created)
        self.assertEqual(record["audit_id"], "AUD-000001")

    def test_frozen_record_is_readable(self):
        record, _ = self.store.create(payload())
        fetched = self.store.get(record["audit_id"])
        self.assertEqual(fetched["input"], payload())
        self.assertEqual(fetched["status"], "unique")
        self.assertIn("conclusion", fetched)
        self.assertIn("evidence", fetched)
        self.assertIsNone(self.store.get("AUD-999999"))


if __name__ == "__main__":
    unittest.main()
