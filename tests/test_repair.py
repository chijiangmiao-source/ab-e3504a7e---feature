import unittest

from app.repair import plan_repair, validate_repair_request
from app.solver import InputError, normalize, solve


def norm(payload):
    return normalize(payload)


def corrections_of(result):
    return {c["id"]: c for c in result["canonical_corrections"]}


def timeline_of(result):
    return {e["id"]: e for e in result["repaired_timeline"]}


class CrossWrapRepairTest(unittest.TestCase):
    def payload(self):
        return {
            "request_id": "cross",
            "modulus": 100,
            "anchor": {"id": "A", "absolute": 95},
            "events": [{"id": "B", "counter": 4}],
            "constraints": [
                {"id": "c1", "source": "A", "target": "B", "lo": 8, "hi": 8},
            ],
        }

    def test_source_is_unsatisfiable(self):
        self.assertEqual(solve(norm(self.payload()))["status"],
                         "unsatisfiable")

    def test_single_tick_down_carries_residue_across_wrap(self):
        result = plan_repair(norm(self.payload()), 1)
        self.assertEqual(result["status"], "repaired")
        b = corrections_of(result)["B"]
        self.assertEqual(b["tick_adjustment"], -1)
        self.assertEqual(b["original_counter"], 4)
        self.assertEqual(b["corrected_counter"], 3)  # (4 - 1) mod 100
        self.assertTrue(b["changed"])
        self.assertEqual(result["objective"]["changed_events"], 1)
        self.assertEqual(result["objective"]["abs_tick_sum"], 1)

    def test_repaired_timeline_unwraps_to_103(self):
        result = plan_repair(norm(self.payload()), 1)
        self.assertEqual(timeline_of(result)["B"]["absolute"], 103)
        self.assertEqual(timeline_of(result)["B"]["wrap"], 1)
        self.assertEqual(timeline_of(result)["A"]["absolute"], 95)

    def test_per_constraint_delta_is_recomputable_and_satisfied(self):
        result = plan_repair(norm(self.payload()), 1)
        delta, = result["constraint_deltas"]
        self.assertEqual(delta["constraint"], "c1")
        self.assertEqual(delta["absolute_delta"], 8)
        self.assertEqual(delta["wrap_difference"], 1)
        self.assertEqual(delta["wrap_bounds"], [1, 1])
        self.assertTrue(delta["satisfied"])

    def test_causal_window_is_never_widened(self):
        # the only repair path changes the reading; the [8, 8] window must be
        # present verbatim in the recomputed deltas
        result = plan_repair(norm(self.payload()), 1)
        self.assertEqual(result["constraint_deltas"][0]["interval"], [8, 8])


class GlobalOptimumTieTest(unittest.TestCase):
    def payload(self):
        return {
            "request_id": "tie",
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

    def test_two_symmetrical_optima_adjudicated_in_identifier_order(self):
        # (-1, 0) and (0, +1) both cost (1 changed, 1 tick); identifier order
        # with ticks ascending from -K picks B = -1.
        result = plan_repair(norm(self.payload()), 1)
        self.assertEqual(result["status"], "repaired")
        c = corrections_of(result)
        self.assertEqual(c["B"]["tick_adjustment"], -1)
        self.assertEqual(c["C"]["tick_adjustment"], 0)
        self.assertEqual(result["objective"]["changed_events"], 1)
        self.assertEqual(result["objective"]["abs_tick_sum"], 1)

    def test_changed_events_beat_absolute_sum(self):
        # (0, -2) uses 1 event / 2 ticks and must beat (1, -1) / 2 events.
        payload = {
            "request_id": "prio",
            "modulus": 100,
            "anchor": {"id": "A", "absolute": 150},
            "events": [
                {"id": "B", "counter": 82},
                {"id": "C", "counter": 87},
            ],
            "constraints": [
                {"id": "c1", "source": "B", "target": "C",
                 "lo": -115, "hi": -97},
                {"id": "c2", "source": "A", "target": "C",
                 "lo": -12, "hi": 58},
            ],
        }
        result = plan_repair(norm(payload), 2)
        c = corrections_of(result)
        self.assertEqual(c["B"]["tick_adjustment"], 0)
        self.assertEqual(c["C"]["tick_adjustment"], -2)
        self.assertEqual(result["objective"], {
            "changed_events": 1,
            "abs_tick_sum": 2,
            "tie_break": "lexicographically smallest vector over event ids",
        })
        for d in result["constraint_deltas"]:
            self.assertTrue(d["satisfied"])

    def test_vector_never_exceeds_budget(self):
        result = plan_repair(norm(self.payload()), 1)
        for c in result["canonical_corrections"]:
            self.assertLessEqual(abs(c["tick_adjustment"]), 1)


class BeyondBudgetTest(unittest.TestCase):
    def payload(self):
        return {
            "request_id": "exhaust",
            "modulus": 100,
            "anchor": {"id": "A", "absolute": 95},
            "events": [{"id": "B", "counter": 3}],
            "constraints": [
                {"id": "c1", "source": "A", "target": "B",
                 "lo": 8, "hi": 8},
                {"id": "c2", "source": "B", "target": "A",
                 "lo": -108, "hi": -108},
            ],
        }

    def test_every_branch_dies_within_budget(self):
        result = plan_repair(norm(self.payload()), 1)
        self.assertEqual(result["status"], "beyond_budget")
        self.assertEqual(result["search"]["budget_K"], 1)

    def test_exhaustion_evidence_is_recomputable(self):
        result = plan_repair(norm(self.payload()), 1)
        pruning = result["search"]["tree"]["domain_pruning"]
        removed = {p["event"]: p for p in pruning}["B"]["removed_ticks"]
        self.assertEqual(removed, [-1, 0, 1])
        witnesses = {p["event"]: p for p in pruning}["B"]["witnesses"]
        self.assertEqual(len(witnesses), 3)
        for w in witnesses:
            self.assertIn(w["kind"],
                          {"empty_wrap_interval", "negative_cycle"})
            if w["kind"] == "negative_cycle":
                self.assertEqual(
                    w["total_weight"],
                    sum(s["weight"] for s in w["steps"]))
                self.assertLess(w["total_weight"], 0)
                self.assertEqual(w["cycle"][0], w["cycle"][-1])
            else:
                self.assertGreater(w["wrap_lower"], w["wrap_upper"])
        self.assertGreaterEqual(
            result["search"]["stats"]["infeasible_prefixes"]
            + result["search"]["stats"]["infeasible_leaves"], 1)

    def test_zero_budget_on_unsat_is_beyond_budget(self):
        result = plan_repair(norm(self.payload()), 0)
        self.assertEqual(result["status"], "beyond_budget")


class CorrectedAmbiguityTest(unittest.TestCase):
    def test_ambiguous_repair_reports_canonical_timeline(self):
        payload = {
            "request_id": "amb",
            "modulus": 100,
            "anchor": {"id": "A", "absolute": 95},
            "events": [
                {"id": "B", "counter": 18},
                {"id": "C", "counter": 43},
            ],
            "constraints": [
                {"id": "c1", "source": "A", "target": "B",
                 "lo": 20, "hi": 138},
                {"id": "c2", "source": "B", "target": "C",
                 "lo": 129, "hi": 140},
            ],
        }
        result = plan_repair(norm(payload), 2)
        self.assertEqual(result["status"], "repaired")
        self.assertEqual(result["repaired_status"], "ambiguous")
        tl = timeline_of(result)
        self.assertEqual(tl["B"]["absolute"], 116)
        self.assertEqual(tl["C"]["absolute"], 245)


class RequestValidationTest(unittest.TestCase):
    def test_K_must_be_integer(self):
        with self.assertRaises(InputError) as ctx:
            validate_repair_request({"fix_id": "f", "K": 1.0}, 100)
        self.assertTrue(any("K must be an integer" in p
                            for p in ctx.exception.problems))

    def test_K_cannot_exceed_half_modulus(self):
        with self.assertRaises(InputError) as ctx:
            validate_repair_request({"fix_id": "f", "K": 51}, 100)
        self.assertTrue(any("[0, 50]" in p for p in ctx.exception.problems))

    def test_K_half_modulus_is_allowed(self):
        req = validate_repair_request({"fix_id": "f", "K": 50}, 100)
        self.assertEqual(req["K"], 50)

    def test_negative_K_and_boolean_K_rejected(self):
        with self.assertRaises(InputError):
            validate_repair_request({"fix_id": "f", "K": -1}, 100)
        with self.assertRaises(InputError):
            validate_repair_request({"fix_id": "f", "K": True}, 100)

    def test_fix_id_must_be_non_empty_string(self):
        with self.assertRaises(InputError):
            validate_repair_request({"fix_id": "", "K": 1}, 100)

    def test_unknown_fields_rejected(self):
        with self.assertRaises(InputError) as ctx:
            validate_repair_request(
                {"fix_id": "f", "K": 1, "widen_windows": True}, 100)
        self.assertTrue(any("unknown field" in p
                            for p in ctx.exception.problems))

    def test_body_must_be_object(self):
        with self.assertRaises(InputError):
            validate_repair_request([], 100)


if __name__ == "__main__":
    unittest.main()
