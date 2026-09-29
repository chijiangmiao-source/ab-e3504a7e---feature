"""One-shot acceptance harness for the deep-space audit service.

Runs three gates and reports the outcome through the process exit code:

1. build check -- every Python source compiles;
2. code tests  -- the unit-test suite (solver, repair search, store, API);
3. API smoke   -- HTTP checks against a live service at APP_URL covering the
   reference unwrap (B=103), the ambiguous twin timelines, the bidirectional
   conflict chain, idempotent record creation, counter repairs (cross-wrap
   correction, global-optimum tie adjudication, within-budget exhaustion),
   repair idempotency and a legacy-audit regression pass.

Exit code 0 means every check passed.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_URL = os.environ.get("APP_URL", "http://127.0.0.1:8080").rstrip("/")

RESULTS = []

# unique per process so re-running the harness against a long-lived service
# exercises fresh 201 creation rather than idempotent 200 replay
RUN_ID = str(int(time.time() * 1000))[-10:]


def rid(value):
    return f"{value}-{RUN_ID}"


def check(name, ok, detail=""):
    RESULTS.append(bool(ok))
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"
    if detail:
        line += f" -- {detail}"
    print(line, flush=True)


def expect(condition, message):
    if not condition:
        raise AssertionError(message)


def request(method, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        APP_URL + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


# ---------------------------------------------------------------------------
# gate 1 + 2: build check and code tests
# ---------------------------------------------------------------------------

def gate_build():
    proc = subprocess.run(
        [sys.executable, "-m", "compileall", "-q", "app", "tests", "verify"],
        cwd=ROOT, capture_output=True, text=True)
    detail = ""
    if proc.returncode != 0:
        lines = (proc.stderr or proc.stdout).strip().splitlines()
        detail = lines[-1] if lines else "compileall failed"
    check("build: all sources compile", proc.returncode == 0, detail)


def gate_tests():
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=ROOT, capture_output=True, text=True)
    lines = (proc.stderr + proc.stdout).strip().splitlines()
    check("code: unit test suite", proc.returncode == 0,
          lines[-1] if lines else "")


# ---------------------------------------------------------------------------
# gate 3: HTTP smoke against the live service
# ---------------------------------------------------------------------------

UNIQUE_PAYLOAD = {
    "request_id": rid("smoke-unique-1"),
    "modulus": 100,
    "anchor": {"id": "A", "absolute": 95},
    "events": [{"id": "B", "counter": 3}],
    "constraints": [
        {"id": "c1", "source": "A", "target": "B", "lo": 8, "hi": 8},
    ],
}

AMBIGUOUS_PAYLOAD = {
    "request_id": rid("smoke-ambiguous-1"),
    "modulus": 100,
    "anchor": {"id": "A", "absolute": 95},
    "events": [{"id": "B", "counter": 3}, {"id": "C", "counter": 50}],
    "constraints": [
        {"id": "c1", "source": "A", "target": "B", "lo": 8, "hi": 108},
        {"id": "c2", "source": "B", "target": "C", "lo": -53, "hi": 47},
    ],
}

CONFLICT_PAYLOAD = {
    "request_id": rid("smoke-conflict-1"),
    "modulus": 100,
    "anchor": {"id": "A", "absolute": 95},
    "events": [{"id": "B", "counter": 3}],
    "constraints": [
        {"id": "c1", "source": "A", "target": "B", "lo": 8, "hi": 8},
        {"id": "c2", "source": "B", "target": "A", "lo": -108, "hi": -108},
    ],
}

# counter reading is off by one tick on the wrap boundary
REPAIR_CROSS_PAYLOAD = {
    "request_id": rid("smoke-repair-cross"),
    "modulus": 100,
    "anchor": {"id": "A", "absolute": 95},
    "events": [{"id": "B", "counter": 4}],
    "constraints": [
        {"id": "c1", "source": "A", "target": "B", "lo": 8, "hi": 8},
    ],
}

# (-1, 0) and (0, +1) are tied global optima at (1 changed, 1 tick)
REPAIR_TIE_PAYLOAD = {
    "request_id": rid("smoke-repair-tie"),
    "modulus": 100,
    "anchor": {"id": "A", "absolute": 0},
    "events": [
        {"id": "B", "counter": 16},
        {"id": "C", "counter": 3},
    ],
    "constraints": [
        {"id": "c1", "source": "A", "target": "B", "lo": -12, "hi": 25},
        {"id": "c2", "source": "B", "target": "C", "lo": 88, "hi": 139},
    ],
}

# 1 event x 2 ticks must beat 2 events x 1 tick (changed count dominates)
REPAIR_PRIORITY_PAYLOAD = {
    "request_id": rid("smoke-repair-prio"),
    "modulus": 100,
    "anchor": {"id": "A", "absolute": 150},
    "events": [
        {"id": "B", "counter": 82},
        {"id": "C", "counter": 87},
    ],
    "constraints": [
        {"id": "c1", "source": "B", "target": "C", "lo": -115, "hi": -97},
        {"id": "c2", "source": "A", "target": "C", "lo": -12, "hi": 58},
    ],
}

# no counter move within K=1 can break the bidirectional contradiction
REPAIR_EXHAUST_PAYLOAD = {
    "request_id": rid("smoke-repair-exhaust"),
    "modulus": 100,
    "anchor": {"id": "A", "absolute": 95},
    "events": [{"id": "B", "counter": 3}],
    "constraints": [
        {"id": "c1", "source": "A", "target": "B", "lo": 8, "hi": 8},
        {"id": "c2", "source": "B", "target": "A", "lo": -108, "hi": -108},
    ],
}


def timeline_map(timeline):
    return {e["id"]: e for e in timeline}


def wait_ready():
    for _ in range(60):
        try:
            status, _body = request("GET", "/health")
            if status == 200:
                return True
        except OSError:
            pass
        time.sleep(1)
    return False


def smoke_health():
    status, body = request("GET", "/health")
    expect(status == 200, f"health returned {status}")
    expect(body.get("status") == "ok", f"unexpected health body {body}")


def smoke_unique_unwrap():
    status, body = request("POST", "/audits", UNIQUE_PAYLOAD)
    expect(status == 201, f"create returned {status}: {body}")
    expect(body.get("status") == "unique", f"status={body.get('status')}")
    expect(body.get("replayed") is False, "first create must not be a replay")
    timeline = timeline_map(body["conclusion"]["timeline"])
    expect(timeline["B"]["absolute"] == 103,
           f"B unwrapped to {timeline['B']['absolute']}, expected 103")
    expect(timeline["B"]["wrap"] == 1, "B wrap count must be 1")
    expect(timeline["A"]["absolute"] == 95, "anchor must stay at 95")


def smoke_frozen_record():
    status, created = request("POST", "/audits",
                              {**UNIQUE_PAYLOAD, "request_id": rid("smoke-frozen-1")})
    expect(status == 201, f"create returned {status}")
    status, fetched = request("GET", f"/audits/{created['audit_id']}")
    expect(status == 200, f"read returned {status}")
    expect(fetched["input"] == {**UNIQUE_PAYLOAD, "request_id": rid("smoke-frozen-1")},
           "frozen input does not match the submitted payload")
    expect(fetched["conclusion"] == created["conclusion"],
           "frozen conclusion changed between create and read")
    expect("evidence" in fetched and "derivation" in fetched["evidence"],
           "frozen evidence missing")


def smoke_ambiguous_timelines():
    status, body = request("POST", "/audits", AMBIGUOUS_PAYLOAD)
    expect(status == 201, f"create returned {status}: {body}")
    expect(body.get("status") == "ambiguous", f"status={body.get('status')}")
    first, second = body["conclusion"]["timelines"]
    t1, t2 = timeline_map(first), timeline_map(second)
    expect(t1["B"]["absolute"] == 103 and t1["C"]["absolute"] == 50,
           f"timeline 1 unexpected: {first}")
    expect(t2["B"]["absolute"] == 103 and t2["C"]["absolute"] == 150,
           f"timeline 2 unexpected: {second}")
    rel = body["conclusion"]["first_unstable_relation"]
    expect(rel is not None, "ambiguous case must report an unstable relation")
    expect(rel["events"] == ["A", "C"], f"unstable pair: {rel['events']}")
    expect(rel["in_timeline_1"] == "after", "A must follow C in timeline 1")
    expect(rel["in_timeline_2"] == "before", "A must precede C in timeline 2")


def smoke_conflict_chain():
    status, body = request("POST", "/audits", CONFLICT_PAYLOAD)
    expect(status == 201, f"create returned {status}: {body}")
    expect(body.get("status") == "unsatisfiable", f"status={body.get('status')}")
    chain = body["conclusion"]["conflict_chain"]
    expect(chain["constraints"] == ["c1", "c2"],
           f"chain constraints: {chain['constraints']}")
    total = sum(step["weight"] for step in chain["steps"])
    expect(chain["total_weight"] == total,
           "chain weights do not recompute to the reported total")
    expect(total < 0, "conflict chain must close with a negative total")
    expect(chain["cycle"][0] == chain["cycle"][-1], "chain must be a cycle")


def smoke_idempotency():
    _, before = request("GET", "/audits")
    payload = {**UNIQUE_PAYLOAD, "request_id": rid("smoke-idem-1")}
    status, created = request("POST", "/audits", payload)
    expect(status == 201, f"create returned {status}")

    status, replay = request("POST", "/audits", copy.deepcopy(payload))
    expect(status == 200, f"replay returned {status}")
    expect(replay["replayed"] is True, "replay must be flagged")
    expect(replay["audit_id"] == created["audit_id"],
           "replay must return the original audit id")

    changed = copy.deepcopy(payload)
    changed["constraints"][0]["hi"] = 9
    status, conflict = request("POST", "/audits", changed)
    expect(status == 409, f"changed payload returned {status}, expected 409")
    expect(conflict.get("existing_audit_id") == created["audit_id"],
           "409 must reference the original audit")

    _, after = request("GET", "/audits")
    expect(after["count"] == before["count"] + 1,
           f"record count moved {before['count']} -> {after['count']}; "
           "the rejected retry must not add a record")


def smoke_validation_and_404():
    disconnected = {
        "request_id": rid("smoke-invalid-1"),
        "modulus": 100,
        "anchor": {"id": "A", "absolute": 95},
        "events": [{"id": "B", "counter": 3}, {"id": "Z", "counter": 1}],
        "constraints": [
            {"id": "c1", "source": "A", "target": "B", "lo": 8, "hi": 8},
        ],
    }
    status, body = request("POST", "/audits", disconnected)
    expect(status == 400, f"disconnected event returned {status}")
    expect(body.get("error") == "invalid_input", f"error body: {body}")

    status, _ = request("GET", "/audits/AUD-000000")
    expect(status == 404, f"unknown audit returned {status}")


def _create(payload):
    status, body = request("POST", "/audits", payload)
    expect(status == 201, f"create returned {status}: {body}")
    expect(body.get("status") == "unsatisfiable",
           f"repair source must be unsatisfiable, got {body.get('status')}")
    return body


def _post_repair(audit_id, fix_id, K):
    return request("POST", f"/audits/{audit_id}/repairs",
                   {"fix_id": fix_id, "K": K})


def smoke_repair_cross_wrap():
    created = _create(REPAIR_CROSS_PAYLOAD)
    status, body = _post_repair(created["audit_id"], rid("smoke-fix-cross"), 1)
    expect(status == 201, f"repair returned {status}: {body}")
    expect(body.get("status") == "repaired",
          f"repair status={body.get('status')}")
    corrections = {c["id"]: c for c in body["canonical_corrections"]}
    expect(corrections["B"]["tick_adjustment"] == -1,
           f"expected tick -1, got {corrections['B']}")
    expect(corrections["B"]["corrected_counter"] == 3,
           "corrected reading must renormalise to 3")
    expect(body["objective"] == {
        "changed_events": 1, "abs_tick_sum": 1,
        "tie_break": "lexicographically smallest vector over event ids"},
          f"objective: {body['objective']}")
    timeline = timeline_map(body["repaired_timeline"])
    expect(timeline["B"]["absolute"] == 103 and timeline["B"]["wrap"] == 1,
           f"repaired timeline: {timeline}")
    for delta in body["constraint_deltas"]:
        expect(delta["satisfied"] is True, f"delta not satisfied: {delta}")
        expect(delta["interval"] == [8, 8],
               "causal window must remain [8, 8], never widened")
        total = delta["absolute_delta"]
        expect(8 <= total <= 8, f"absolute delta {total} outside [8,8]")
    # the source audit itself is untouched
    status, fetched = request("GET", f"/audits/{created['audit_id']}")
    expect(fetched["status"] == "unsatisfiable",
           "source audit must stay unsatisfiable after a repair")
    expect("canonical_corrections" not in fetched,
           "source audit must not be rewritten with repair data")
    # repair is readable by its own id
    status, fix = request("GET", f"/repairs/{body['repair_id']}")
    expect(status == 200 and fix["fix_id"] == rid("smoke-fix-cross"),
          f"repair readback failed: {status}")


def smoke_repair_global_tie():
    created = _create(REPAIR_TIE_PAYLOAD)
    status, body = _post_repair(created["audit_id"], rid("smoke-fix-tie"), 1)
    expect(status == 201, f"repair returned {status}: {body}")
    vec = {c["id"]: c["tick_adjustment"]
           for c in body["canonical_corrections"]}
    expect(vec == {"B": -1, "C": 0},
           f"tie must resolve to (-1, 0) in identifier order, got {vec}")
    expect(body["objective"]["changed_events"] == 1
           and body["objective"]["abs_tick_sum"] == 1,
           f"objective: {body['objective']}")


def smoke_repair_changed_count_first():
    created = _create(REPAIR_PRIORITY_PAYLOAD)
    status, body = _post_repair(created["audit_id"], rid("smoke-fix-prio"), 2)
    expect(status == 201, f"repair returned {status}: {body}")
    vec = {c["id"]: c["tick_adjustment"]
           for c in body["canonical_corrections"]}
    # (B=0, C=-2): one changed event beats the (B=1, C=-1) two-event fix
    expect(vec == {"B": 0, "C": -2},
           f"expected (0, -2), got {vec}")
    expect(body["objective"]["changed_events"] == 1,
           "changed-event count must dominate absolute tick sum")
    expect(body["objective"]["abs_tick_sum"] == 2,
           f"absolute tick sum: {body['objective']}")


def smoke_repair_beyond_budget():
    created = _create(REPAIR_EXHAUST_PAYLOAD)
    status, body = _post_repair(created["audit_id"], rid("smoke-fix-exhaust"), 1)
    expect(status == 201, f"repair returned {status}: {body}")
    expect(body.get("status") == "beyond_budget",
          f"expected beyond_budget, got {body.get('status')}")
    tree = body["search"]["tree"]
    removed = {}
    for prune in tree.get("domain_pruning", []):
        removed.setdefault(prune["event"], set()).update(
            prune["removed_ticks"])
    expect(removed.get("B") == {-1, 0, 1},
           f"every tick in [-1,1] must be refuted, got {removed}")
    witnesses = [w for prune in tree.get("domain_pruning", [])
                 for w in prune["witnesses"]]
    expect(witnesses, "exhaustion must carry recomputable branch evidence")
    for w in witnesses:
        if w["kind"] == "negative_cycle":
            expect(w["total_weight"]
                   == sum(s["weight"] for s in w["steps"]) < 0,
                   "conflict-chain weights must recompute to a negative sum")
        else:
            expect(w["wrap_lower"] > w["wrap_upper"],
                   "empty-wrap witness must have lower > upper")
    # same fix_id replays; a changed K is rejected
    status, replay = _post_repair(
        created["audit_id"], rid("smoke-fix-exhaust"), 1)
    expect(status == 200 and replay["replayed"] is True,
          f"replay failed: {status} {replay.get('error')}")
    expect(replay["repair_id"] == body["repair_id"],
          "replay must keep the original repair id")
    status, conflict = _post_repair(
        created["audit_id"], rid("smoke-fix-exhaust"), 0)
    expect(status == 409, f"changed K must return 409, got {status}")
    expect(conflict.get("error") == "fix_id_conflict",
          f"conflict body: {conflict}")


def smoke_repair_validation_and_source_guard():
    guard_source = {**REPAIR_CROSS_PAYLOAD,
                    "request_id": rid("smoke-repair-guard-src")}
    created = _create(guard_source)
    status, body = _post_repair(created["audit_id"], rid("smoke-fix-bad"), 51)
    expect(status == 400, f"K=51 must be rejected, got {status}")
    expect(any("[0, 50]" in p for p in body.get("problems", [])),
          f"problems must cite the half-modulus bound: {body}")
    status, _ = request(
        "POST", f"/audits/{created['audit_id']}/repairs",
        {"fix_id": 9, "K": 1})
    expect(status == 400, "non-string fix_id must be rejected")
    # repairs against a satisfiable audit are refused
    unique = {
        "request_id": rid("smoke-fix-guard"),
        "modulus": 100,
        "anchor": {"id": "A", "absolute": 95},
        "events": [{"id": "B", "counter": 3}],
        "constraints": [
            {"id": "c1", "source": "A", "target": "B", "lo": 8, "hi": 8}],
    }
    status, ok = request("POST", "/audits", unique)
    expect(status == 201 and ok["status"] == "unique", "guard setup failed")
    status, refused = _post_repair(ok["audit_id"], rid("smoke-fix-guard"), 1)
    expect(status == 409
           and refused.get("error") == "source_not_unsatisfiable",
           f"repairs on satisfiable audits must be refused: {status}")


def smoke_legacy_audit_regression():
    # original create/read/list semantics survive unchanged after repairs
    _, audits_before = request("GET", "/audits")
    status, created = request("POST", "/audits", {
        **UNIQUE_PAYLOAD, "request_id": rid("smoke-legacy-1")})
    expect(status == 201, f"legacy create returned {status}")
    status, fetched = request("GET", f"/audits/{created['audit_id']}")
    expect(status == 200 and fetched["status"] == "unique",
          "legacy read must still return the frozen unique record")
    status, listing = request("GET", "/audits")
    expect(listing["count"] == audits_before["count"] + 1,
           "legacy audit list count must advance by one")
    # an unsatisfiable record repaired earlier is byte-for-byte stable
    status, conflict_audit = request("POST", "/audits", {
        **CONFLICT_PAYLOAD, "request_id": rid("smoke-legacy-conflict")})
    expect(status == 201 and conflict_audit["status"] == "unsatisfiable",
          "legacy conflict creation must still work")
    before = conflict_audit["conclusion"]
    _post_repair(conflict_audit["audit_id"], rid("smoke-legacy-fix"), 1)
    status, reread = request("GET", f"/audits/{conflict_audit['audit_id']}")
    expect(reread["conclusion"] == before,
           "the legacy conflict chain must be stable after a repair")


def gate_http():
    if not wait_ready():
        check("http: service reachable", False, f"no /health from {APP_URL}")
        return
    check("http: service reachable", True, APP_URL)
    smokes = [
        ("http: health", smoke_health),
        ("http: reference unwrap B=103", smoke_unique_unwrap),
        ("http: frozen record readable", smoke_frozen_record),
        ("http: ambiguous twin timelines", smoke_ambiguous_timelines),
        ("http: bidirectional conflict chain", smoke_conflict_chain),
        ("http: idempotent records", smoke_idempotency),
        ("http: validation and 404", smoke_validation_and_404),
        ("http: repair across a wrap boundary", smoke_repair_cross_wrap),
        ("http: repair global-optimum tie adjudication",
         smoke_repair_global_tie),
        ("http: repair changed-count beats tick sum",
         smoke_repair_changed_count_first),
        ("http: repair within-budget exhaustion evidence",
         smoke_repair_beyond_budget),
        ("http: repair validation and source guard",
         smoke_repair_validation_and_source_guard),
        ("http: legacy audit semantics regression",
         smoke_legacy_audit_regression),
    ]
    for name, fn in smokes:
        try:
            fn()
        except AssertionError as exc:
            check(name, False, str(exc))
        except Exception as exc:  # keep reporting the remaining checks
            check(name, False, f"{type(exc).__name__}: {exc}")
        else:
            check(name, True)


def main():
    print(f"acceptance target: {APP_URL}", flush=True)
    gate_build()
    gate_tests()
    gate_http()
    passed = sum(RESULTS)
    total = len(RESULTS)
    ok = passed == total
    print(f"acceptance: {'OK' if ok else 'FAILED'} "
          f"({passed}/{total} checks passed)", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
