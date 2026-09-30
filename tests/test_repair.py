import unittest

from app.repair import normalize_repair, solve_repair
from app.solver import InputError, normalize, solve


def repair(payload, k, repair_id="fix-1"):
    norm = normalize(payload)
    assert solve(norm)["status"] == "unsatisfiable"
    req = normalize_repair({"repair_id": repair_id, "k": k}, norm)
    return solve_repair(req), norm


def corrections_map(conclusion):
    return {row["id"]: row for row in conclusion["canonical_corrections"]}


def timeline_map(conclusion):
    return {t["id"]: t for t in conclusion["timeline"]}


class RepairValidationTest(unittest.TestCase):
    def payload(self):
        return {
            "request_id": "r",
            "modulus": 100,
            "anchor": {"id": "A", "absolute": 95},
            "events": [{"id": "B", "counter": 3}],
            "constraints": [
                {"id": "c1", "source": "A", "target": "B", "lo": 9, "hi": 9},
            ],
        }

    def test_k_above_half_modulus_rejected(self):
        norm = normalize(self.payload())
        with self.assertRaises(InputError) as ctx:
            normalize_repair({"repair_id": "f", "k": 51}, norm)
        self.assertTrue(any("[0, 50]" in p for p in ctx.exception.problems),
                        ctx.exception.problems)

    def test_k_negative_rejected(self):
        norm = normalize(self.payload())
        with self.assertRaises(InputError) as ctx:
            normalize_repair({"repair_id": "f", "k": -1}, norm)
        self.assertTrue(any("k must be" in p for p in ctx.exception.problems))

    def test_k_must_be_integer(self):
        norm = normalize(self.payload())
        with self.assertRaises(InputError) as ctx:
            normalize_repair({"repair_id": "f", "k": 1.5}, norm)
        self.assertTrue(any("k must be an integer" in p
                            for p in ctx.exception.problems))

    def test_half_modulus_is_allowed(self):
        norm = normalize(self.payload())
        req = normalize_repair({"repair_id": "f", "k": 50}, norm)
        self.assertEqual(req.k, 50)

    def test_repair_id_required(self):
        norm = normalize(self.payload())
        with self.assertRaises(InputError) as ctx:
            normalize_repair({"k": 1}, norm)
        self.assertTrue(any("repair_id" in p for p in ctx.exception.problems))

    def test_unknown_field_rejected(self):
        norm = normalize(self.payload())
        with self.assertRaises(InputError) as ctx:
            normalize_repair({"repair_id": "f", "k": 1, "widen": True}, norm)
        self.assertTrue(any("unknown field 'widen'" in p
                            for p in ctx.exception.problems))

    def test_non_object_body(self):
        norm = normalize(self.payload())
        with self.assertRaises(InputError):
            normalize_repair([1, 2], norm)


class RepairOptimumTest(unittest.TestCase):
    def test_single_positive_tick_within_wrap(self):
        """lo=9 with B=3 only fits after correcting B to 4 (B unwraps 104)."""
        payload = {
            "request_id": "r", "modulus": 100,
            "anchor": {"id": "A", "absolute": 95},
            "events": [{"id": "B", "counter": 3}],
            "constraints": [
                {"id": "c1", "source": "A", "target": "B", "lo": 9, "hi": 9}],
        }
        (status, conclusion, evidence), _ = repair(payload, 1)
        self.assertEqual(status, "repaired")
        self.assertEqual(corrections_map(conclusion)["B"]["correction"], 1)
        self.assertEqual(corrections_map(conclusion)["B"]["corrected_counter"], 4)
        self.assertEqual(conclusion["changed_count"], 1)
        self.assertEqual(conclusion["abs_correction_sum"], 1)
        self.assertEqual(timeline_map(conclusion)["B"]["absolute"], 104)
        self.assertTrue(all(d["satisfied"]
                            for d in conclusion["constraint_deltas"]))
        self.assertTrue(evidence["search"]["complete"])

    def test_cross_wrap_negative_tick_normalizes_to_modulus_minus_one(self):
        """B=0 must read 99 (same wrap as A=95): delta -1 normalizes to 99."""
        payload = {
            "request_id": "r", "modulus": 100,
            "anchor": {"id": "A", "absolute": 95},
            "events": [{"id": "B", "counter": 0}],
            "constraints": [
                {"id": "c1", "source": "A", "target": "B", "lo": 4, "hi": 4}],
        }
        (status, conclusion, _evidence), _ = repair(payload, 1)
        self.assertEqual(status, "repaired")
        row = corrections_map(conclusion)["B"]
        self.assertEqual(row["correction"], -1)
        self.assertEqual(row["original_counter"], 0)
        self.assertEqual(row["corrected_counter"], 99)
        self.assertEqual(timeline_map(conclusion)["B"]["absolute"], 99)
        self.assertEqual(timeline_map(conclusion)["B"]["wrap"], 0)

    def test_lexicographic_tie_on_same_residue(self):
        """M=2: delta -1 and +1 give the same residue 1; vector order picks -1."""
        payload = {
            "request_id": "r", "modulus": 2,
            "anchor": {"id": "A", "absolute": 0},
            "events": [{"id": "B", "counter": 0}],
            "constraints": [
                {"id": "c1", "source": "A", "target": "B", "lo": 1, "hi": 1}],
        }
        (status, conclusion, _evidence), _ = repair(payload, 1)
        self.assertEqual(status, "repaired")
        self.assertEqual(corrections_map(conclusion)["B"]["correction"], -1)
        self.assertEqual(conclusion["changed_count"], 1)
        self.assertEqual(conclusion["abs_correction_sum"], 1)

    def test_lexicographic_tie_between_plus_and_minus_k(self):
        """M=4: target residue 2 is reached by delta +2 or -2; -2 wins."""
        payload = {
            "request_id": "r", "modulus": 4,
            "anchor": {"id": "A", "absolute": 0},
            "events": [
                {"id": "B", "counter": 0},
                {"id": "C", "counter": 0}],
            "constraints": [
                {"id": "c1", "source": "A", "target": "B", "lo": 2, "hi": 2},
                {"id": "c2", "source": "B", "target": "C", "lo": 2, "hi": 2}],
        }
        (status, conclusion, _evidence), _ = repair(payload, 2)
        self.assertEqual(status, "repaired")
        vector = [c["correction"]
                  for c in conclusion["canonical_corrections"]]
        self.assertEqual(vector, [-2, 0])
        self.assertEqual(conclusion["changed_count"], 1)
        self.assertEqual(conclusion["abs_correction_sum"], 2)

    def test_minimize_changed_events_before_absolute_sum(self):
        """The chain forces B to move (+1) but C already reads correctly."""
        payload = {
            "request_id": "r", "modulus": 10,
            "anchor": {"id": "A", "absolute": 0},
            "events": [
                {"id": "B", "counter": 0},
                {"id": "C", "counter": 9}],
            "constraints": [
                {"id": "c1", "source": "A", "target": "B", "lo": 1, "hi": 1},
                {"id": "c2", "source": "B", "target": "C", "lo": 8, "hi": 8}],
        }
        (status, conclusion, _evidence), _ = repair(payload, 3)
        self.assertEqual(status, "repaired")
        self.assertEqual(conclusion["changed_events"], ["B"])
        self.assertEqual(conclusion["changed_count"], 1)
        self.assertEqual(conclusion["abs_correction_sum"], 1)
        self.assertEqual(corrections_map(conclusion)["B"]["correction"], 1)
        self.assertEqual(corrections_map(conclusion)["C"]["correction"], 0)

    def test_constraint_deltas_are_recomputable_and_satisfied(self):
        payload = {
            "request_id": "r", "modulus": 100,
            "anchor": {"id": "A", "absolute": 95},
            "events": [{"id": "B", "counter": 3}],
            "constraints": [
                {"id": "c1", "source": "A", "target": "B", "lo": 9, "hi": 9}],
        }
        (status, conclusion, _evidence), _ = repair(payload, 1)
        (delta,) = conclusion["constraint_deltas"]
        self.assertEqual(delta["constraint"], "c1")
        self.assertEqual(delta["corrected_counter_delta"], 4 - 95)
        self.assertEqual(delta["wrap_bounds"], [1, 1])
        self.assertEqual(delta["wrap_delta"], 1)
        self.assertEqual(delta["absolute_delta"], 104 - 95)
        self.assertTrue(delta["satisfied"])

    def test_k_zero_on_unsat_exhausts(self):
        payload = {
            "request_id": "r", "modulus": 100,
            "anchor": {"id": "A", "absolute": 95},
            "events": [{"id": "B", "counter": 3}],
            "constraints": [
                {"id": "c1", "source": "A", "target": "B", "lo": 9, "hi": 9}],
        }
        (status, conclusion, _evidence), _ = repair(payload, 0)
        self.assertEqual(status, "exhausted")
        self.assertTrue(conclusion["exhausted"]["complete_proof"])

    def test_self_loop_contradiction_cannot_be_papered_over(self):
        """A self-loop demanding abs(B)-abs(B) in [5,5] is uncorrectable."""
        payload = {
            "request_id": "r", "modulus": 100,
            "anchor": {"id": "A", "absolute": 95},
            "events": [{"id": "B", "counter": 3}],
            "constraints": [
                {"id": "c0", "source": "A", "target": "B", "lo": 8, "hi": 8},
                {"id": "c1", "source": "B", "target": "B", "lo": 5, "hi": 5}],
        }
        (status, conclusion, _evidence), _ = repair(payload, 50)
        self.assertEqual(status, "exhausted")
        self.assertTrue(conclusion["exhausted"]["complete_proof"])


class RepairExhaustionTest(unittest.TestCase):
    def bidirectional_payload(self):
        return {
            "request_id": "r", "modulus": 100,
            "anchor": {"id": "A", "absolute": 95},
            "events": [{"id": "B", "counter": 3}],
            "constraints": [
                {"id": "c1", "source": "A", "target": "B", "lo": 8, "hi": 8},
                {"id": "c2", "source": "B", "target": "A",
                 "lo": -108, "hi": -108}],
        }

    def test_no_solution_within_k_returns_recomputable_evidence(self):
        (status, conclusion, evidence), _ = repair(
            self.bidirectional_payload(), 2)
        self.assertEqual(status, "exhausted")
        ex = conclusion["exhausted"]
        self.assertTrue(ex["complete_proof"])
        self.assertEqual(ex["k"], 2)
        self.assertGreaterEqual(ex["nodes_explored"], 0)
        self.assertIn("sample_branches", ex)
        self.assertTrue(ex["sample_branches"], "exhaustion needs branch evidence")
        for sample in ex["sample_branches"]:
            chain = sample["conflict_chain"]
            self.assertLess(chain["total_weight"], 0)
            self.assertEqual(
                chain["total_weight"],
                sum(step["weight"] for step in chain["steps"]))
            self.assertEqual(chain["cycle"][0], chain["cycle"][-1])
        self.assertEqual(evidence["search"]["complete"], True)

    def test_impossible_even_at_half_modulus(self):
        # c1 needs residue 3 at wrap 1, c2 needs residue 3 at wrap 2: no
        # correction changes the wrap each constraint forces.
        (status, conclusion, _evidence), _ = repair(
            self.bidirectional_payload(), 50)
        self.assertEqual(status, "exhausted")
        self.assertTrue(conclusion["exhausted"]["complete_proof"])
        self.assertTrue(conclusion["exhausted"]["sample_branches"])


if __name__ == "__main__":
    unittest.main()
