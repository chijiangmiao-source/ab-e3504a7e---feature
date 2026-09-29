import unittest

from app.solver import InputError, normalize, solve


def solve_payload(payload):
    return solve(normalize(payload))


def entry(timeline, node_id):
    return next(e for e in timeline if e["id"] == node_id)


class UniqueCaseTest(unittest.TestCase):
    def test_reference_unwrap(self):
        """M=100, anchor A=95, B=3, A->B=[8,8] must unwrap B to 103."""
        result = solve_payload({
            "request_id": "r1",
            "modulus": 100,
            "anchor": {"id": "A", "absolute": 95},
            "events": [{"id": "B", "counter": 3}],
            "constraints": [
                {"id": "c1", "source": "A", "target": "B", "lo": 8, "hi": 8},
            ],
        })
        self.assertEqual(result["status"], "unique")
        timeline = result["conclusion"]["timeline"]
        self.assertEqual(entry(timeline, "A")["absolute"], 95)
        self.assertEqual(entry(timeline, "B")["absolute"], 103)
        self.assertEqual(entry(timeline, "B")["wrap"], 1)

    def test_same_counter_is_not_same_instant(self):
        """Equal counter readings may still land on different absolute ticks."""
        result = solve_payload({
            "request_id": "r2",
            "modulus": 100,
            "anchor": {"id": "A", "absolute": 95},
            "events": [
                {"id": "B", "counter": 3},
                {"id": "C", "counter": 3},
            ],
            "constraints": [
                {"id": "c1", "source": "A", "target": "B", "lo": 8, "hi": 8},
                {"id": "c2", "source": "B", "target": "C", "lo": 100, "hi": 100},
            ],
        })
        self.assertEqual(result["status"], "unique")
        timeline = result["conclusion"]["timeline"]
        self.assertEqual(entry(timeline, "B")["absolute"], 103)
        self.assertEqual(entry(timeline, "C")["absolute"], 203)
        self.assertEqual(entry(timeline, "B")["counter"],
                         entry(timeline, "C")["counter"])

    def test_anchor_with_higher_wrap(self):
        result = solve_payload({
            "request_id": "r3",
            "modulus": 100,
            "anchor": {"id": "A", "absolute": 250},
            "events": [{"id": "B", "counter": 60}],
            "constraints": [
                {"id": "c1", "source": "A", "target": "B", "lo": 10, "hi": 10},
            ],
        })
        self.assertEqual(result["status"], "unique")
        timeline = result["conclusion"]["timeline"]
        self.assertEqual(entry(timeline, "A")["wrap"], 2)
        self.assertEqual(entry(timeline, "B")["absolute"], 260)


class AmbiguousCaseTest(unittest.TestCase):
    def payload(self):
        return {
            "request_id": "r-ambig",
            "modulus": 100,
            "anchor": {"id": "A", "absolute": 95},
            "events": [
                {"id": "B", "counter": 3},
                {"id": "C", "counter": 50},
            ],
            "constraints": [
                {"id": "c1", "source": "A", "target": "B", "lo": 8, "hi": 108},
                {"id": "c2", "source": "B", "target": "C", "lo": -53, "hi": 47},
            ],
        }

    def test_two_canonical_timelines_in_identifier_order(self):
        result = solve_payload(self.payload())
        self.assertEqual(result["status"], "ambiguous")
        first, second = result["conclusion"]["timelines"]
        # lexicographically smallest: B=103 (wrap 1), C=50 (wrap 0)
        self.assertEqual(entry(first, "B")["absolute"], 103)
        self.assertEqual(entry(first, "C")["absolute"], 50)
        # second: C advances one wrap while B stays put
        self.assertEqual(entry(second, "B")["absolute"], 103)
        self.assertEqual(entry(second, "C")["absolute"], 150)
        self.assertNotEqual(
            [(e["id"], e["absolute"]) for e in first],
            [(e["id"], e["absolute"]) for e in second])

    def test_first_unstable_relation(self):
        result = solve_payload(self.payload())
        rel = result["conclusion"]["first_unstable_relation"]
        self.assertIsNotNone(rel)
        self.assertEqual(rel["events"], ["A", "C"])
        self.assertEqual(rel["in_timeline_1"], "after")   # 95 > 50
        self.assertEqual(rel["in_timeline_2"], "before")  # 95 < 150
        self.assertIn("before", rel["possible_relations"])
        self.assertIn("after", rel["possible_relations"])
        self.assertLess(rel["min_delta"], 0)
        self.assertGreater(rel["max_delta"], 0)

    def test_ranges_expose_ambiguity(self):
        result = solve_payload(self.payload())
        ranges = {r["id"]: r for r in result["evidence"]["node_ranges"]}
        self.assertEqual(ranges["B"]["min_wrap"], 1)
        self.assertEqual(ranges["B"]["max_wrap"], 2)
        self.assertEqual(ranges["C"]["min_wrap"], 0)
        self.assertEqual(ranges["C"]["max_wrap"], 2)
        self.assertEqual(ranges["A"]["min_wrap"], ranges["A"]["max_wrap"])


class UnsatisfiableCaseTest(unittest.TestCase):
    def test_bidirectional_conflict_chain(self):
        """c1 forces B=103 while c2 forces B=203: a two-way contradiction."""
        result = solve_payload({
            "request_id": "r-unsat",
            "modulus": 100,
            "anchor": {"id": "A", "absolute": 95},
            "events": [{"id": "B", "counter": 3}],
            "constraints": [
                {"id": "c1", "source": "A", "target": "B", "lo": 8, "hi": 8},
                {"id": "c2", "source": "B", "target": "A", "lo": -108, "hi": -108},
            ],
        })
        self.assertEqual(result["status"], "unsatisfiable")
        chain = result["conclusion"]["conflict_chain"]
        self.assertEqual(chain["constraints"], ["c1", "c2"])
        self.assertEqual(chain["total_weight"], -1)
        self.assertEqual(
            chain["total_weight"], sum(s["weight"] for s in chain["steps"]))
        self.assertEqual(chain["cycle"][0], chain["cycle"][-1])
        self.assertEqual(len(chain["steps"]), 2)

    def test_self_conflicting_constraint(self):
        """No integer wrap satisfies the interval: singleton conflict chain."""
        result = solve_payload({
            "request_id": "r-self",
            "modulus": 100,
            "anchor": {"id": "A", "absolute": 95},
            "events": [{"id": "B", "counter": 3}],
            "constraints": [
                {"id": "c1", "source": "A", "target": "B", "lo": 9, "hi": 9},
            ],
        })
        self.assertEqual(result["status"], "unsatisfiable")
        chain = result["conclusion"]["conflict_chain"]
        self.assertEqual(chain["constraints"], ["c1"])
        self.assertLess(chain["total_weight"], 0)

    def test_negative_wrap_is_rejected(self):
        """A solution before absolute zero is not a valid timeline."""
        result = solve_payload({
            "request_id": "r-neg",
            "modulus": 100,
            "anchor": {"id": "A", "absolute": 95},
            "events": [{"id": "B", "counter": 3}],
            "constraints": [
                {"id": "c1", "source": "B", "target": "A", "lo": 192, "hi": 192},
            ],
        })
        self.assertEqual(result["status"], "unsatisfiable")
        chain = result["conclusion"]["conflict_chain"]
        kinds = {s["kind"] for s in chain["steps"]}
        self.assertIn("non_negative", kinds)
        self.assertIn("c1", chain["constraints"])


class ValidationTest(unittest.TestCase):
    def assert_invalid(self, payload, fragment):
        with self.assertRaises(InputError) as ctx:
            normalize(payload)
        self.assertTrue(
            any(fragment in p for p in ctx.exception.problems),
            f"expected problem containing {fragment!r}, got {ctx.exception.problems}")

    def base(self):
        return {
            "request_id": "v",
            "modulus": 100,
            "anchor": {"id": "A", "absolute": 95},
            "events": [{"id": "B", "counter": 3}],
            "constraints": [
                {"id": "c1", "source": "A", "target": "B", "lo": 8, "hi": 8},
            ],
        }

    def test_too_many_events(self):
        payload = self.base()
        payload["events"] = [{"id": f"E{i:02d}", "counter": 0} for i in range(13)]
        payload["constraints"] = [
            {"id": f"c{i}", "source": "A", "target": f"E{i:02d}", "lo": 0, "hi": 0}
            for i in range(13)
        ]
        self.assert_invalid(payload, "at most 12 events")

    def test_disconnected_event(self):
        payload = self.base()
        payload["events"].append({"id": "Z", "counter": 1})
        self.assert_invalid(payload, "not connected to the anchor")

    def test_duplicate_event_id(self):
        payload = self.base()
        payload["events"].append({"id": "B", "counter": 5})
        self.assert_invalid(payload, "duplicate event id")

    def test_empty_interval(self):
        payload = self.base()
        payload["constraints"][0]["lo"] = 9
        self.assert_invalid(payload, "is empty")

    def test_counter_out_of_range(self):
        payload = self.base()
        payload["events"][0]["counter"] = 100
        self.assert_invalid(payload, "outside [0, 100)")

    def test_unknown_endpoint(self):
        payload = self.base()
        payload["constraints"][0]["target"] = "Q"
        self.assert_invalid(payload, "not a known node")

    def test_modulus_too_small(self):
        payload = self.base()
        payload["modulus"] = 1
        self.assert_invalid(payload, "modulus")

    def test_unknown_field_rejected(self):
        payload = self.base()
        payload["comment"] = "nope"
        self.assert_invalid(payload, "unknown field")


if __name__ == "__main__":
    unittest.main()
