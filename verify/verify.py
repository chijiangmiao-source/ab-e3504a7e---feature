"""One-shot acceptance harness for the deep-space audit service.

Runs three gates and reports the outcome through the process exit code:

1. build check -- every Python source compiles;
2. code tests  -- the unit-test suite (solver, store, API);
3. API smoke   -- HTTP checks against a live service at APP_URL covering the
   reference unwrap (B=103), the ambiguous twin timelines, the bidirectional
   conflict chain, and idempotent record creation.

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
    "request_id": "smoke-unique-1",
    "modulus": 100,
    "anchor": {"id": "A", "absolute": 95},
    "events": [{"id": "B", "counter": 3}],
    "constraints": [
        {"id": "c1", "source": "A", "target": "B", "lo": 8, "hi": 8},
    ],
}

AMBIGUOUS_PAYLOAD = {
    "request_id": "smoke-ambiguous-1",
    "modulus": 100,
    "anchor": {"id": "A", "absolute": 95},
    "events": [{"id": "B", "counter": 3}, {"id": "C", "counter": 50}],
    "constraints": [
        {"id": "c1", "source": "A", "target": "B", "lo": 8, "hi": 108},
        {"id": "c2", "source": "B", "target": "C", "lo": -53, "hi": 47},
    ],
}

CONFLICT_PAYLOAD = {
    "request_id": "smoke-conflict-1",
    "modulus": 100,
    "anchor": {"id": "A", "absolute": 95},
    "events": [{"id": "B", "counter": 3}],
    "constraints": [
        {"id": "c1", "source": "A", "target": "B", "lo": 8, "hi": 8},
        {"id": "c2", "source": "B", "target": "A", "lo": -108, "hi": -108},
    ],
}

# One-tick cross-wrap correction: reading 3 must be 4 so B unwraps to 104.
REPAIR_UNSAT_PAYLOAD = {
    "request_id": "smoke-repair-unsat-1",
    "modulus": 100,
    "anchor": {"id": "A", "absolute": 95},
    "events": [{"id": "B", "counter": 3}],
    "constraints": [
        {"id": "c1", "source": "A", "target": "B", "lo": 9, "hi": 9},
    ],
}

# Global-optimum tie: with M=4, target residue 2 is reached by delta -2 or
# +2 at identical cardinality and absolute cost; identifier-order tie-break
# on the correction vector selects -2.
REPAIR_TIE_PAYLOAD = {
    "request_id": "smoke-repair-tie-1",
    "modulus": 4,
    "anchor": {"id": "A", "absolute": 0},
    "events": [{"id": "B", "counter": 0}],
    "constraints": [
        {"id": "c1", "source": "A", "target": "B", "lo": 2, "hi": 2},
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
                              {**UNIQUE_PAYLOAD, "request_id": "smoke-frozen-1"})
    expect(status == 201, f"create returned {status}")
    status, fetched = request("GET", f"/audits/{created['audit_id']}")
    expect(status == 200, f"read returned {status}")
    expect(fetched["input"] == {**UNIQUE_PAYLOAD, "request_id": "smoke-frozen-1"},
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
    payload = {**UNIQUE_PAYLOAD, "request_id": "smoke-idem-1"}
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
        "request_id": "smoke-invalid-1",
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


# ---------------------------------------------------------------------------
# repair smokes
# ---------------------------------------------------------------------------

def smoke_repair_cross_wrap():
    status, audit = request("POST", "/audits", REPAIR_UNSAT_PAYLOAD)
    expect(status == 201, f"create returned {status}: {audit}")
    expect(audit["status"] == "unsatisfiable", "repair input must be unsat")
    path = f"/audits/{audit['audit_id']}/repairs"

    status, body = request("POST", path, {"repair_id": "fix-cross", "k": 1})
    expect(status == 201, f"repair returned {status}: {body}")
    expect(body["status"] == "repaired", f"status={body['status']}")
    expect(body["replayed"] is False, "first repair must not be a replay")
    corrections = {c["id"]: c
                   for c in body["conclusion"]["canonical_corrections"]}
    expect(corrections["B"]["correction"] == 1,
           f"canonical correction {corrections['B']}")
    expect(corrections["B"]["corrected_counter"] == 4, "B must read 4")
    expect(body["conclusion"]["changed_count"] == 1, "exactly one event moves")
    expect(body["conclusion"]["abs_correction_sum"] == 1, "effort must be 1")
    timeline = timeline_map(body["conclusion"]["timeline"])
    expect(timeline["B"]["absolute"] == 104,
           f"repaired B absolute {timeline['B']['absolute']}")
    deltas = body["conclusion"]["constraint_deltas"]
    expect(all(d["satisfied"] for d in deltas), "every window still satisfied")
    expect(all(d["interval"] == [9, 9] for d in deltas),
           "the causal window itself must not widen")

    # repair is readable through its own endpoint
    status, fetched = request("GET", f"/repairs/{body['repair_number']}")
    expect(status == 200, f"repair read returned {status}")
    expect(fetched["conclusion"] == body["conclusion"],
           "frozen repair conclusion changed between create and read")


def smoke_repair_tie():
    status, audit = request("POST", "/audits", REPAIR_TIE_PAYLOAD)
    expect(status == 201 and audit["status"] == "unsatisfiable",
           f"tie audit setup failed: {status} {audit}")
    status, body = request(
        "POST", f"/audits/{audit['audit_id']}/repairs",
        {"repair_id": "fix-tie", "k": 2})
    expect(status == 201, f"repair returned {status}: {body}")
    expect(body["status"] == "repaired", f"status={body['status']}")
    (only,) = body["conclusion"]["canonical_corrections"]
    expect(only["id"] == "B" and only["correction"] == -2,
           f"tie must resolve to the smaller vector entry -2, got {only}")
    expect(body["conclusion"]["changed_count"] == 1, "one event changed")
    expect(body["conclusion"]["abs_correction_sum"] == 2, "cost 2 either way")


def smoke_repair_exhausted():
    status, audit = request("POST", "/audits",
                            {**CONFLICT_PAYLOAD,
                             "request_id": "smoke-repair-exhaust-1"})
    expect(status == 201 and audit["status"] == "unsatisfiable",
           "conflict audit must exist and be unsat")
    # the bidirectional contradiction cannot be papered over at small k
    status, body = request(
        "POST", f"/audits/{audit['audit_id']}/repairs",
        {"repair_id": "fix-none", "k": 2})
    expect(status == 201, f"repair returned {status}: {body}")
    expect(body["status"] == "exhausted", f"status={body['status']}")
    ex = body["conclusion"]["exhausted"]
    expect(ex["complete_proof"] is True, "small domain must exhaust completely")
    expect(ex["sample_branches"], "exhaustion must carry branch evidence")
    for sample in ex["sample_branches"]:
        chain = sample.get("conflict_chain")
        expect(chain is not None, "sample branch must carry a conflict chain")
        total = sum(step["weight"] for step in chain["steps"])
        expect(chain["total_weight"] == total,
               "chain weights must recompute to the reported total")
        expect(total < 0, "exhaustion chain must close negative")
        expect(chain["cycle"][0] == chain["cycle"][-1], "chain must be a cycle")


def smoke_repair_idempotency():
    status, audit = request("POST", "/audits",
                            {**REPAIR_UNSAT_PAYLOAD,
                             "request_id": "smoke-repair-idem-1"})
    expect(status == 201, f"create returned {status}")
    path = f"/audits/{audit['audit_id']}/repairs"

    status, first = request("POST", path, {"repair_id": "fix-idem", "k": 1})
    expect(status == 201, f"repair returned {status}")

    status, replay = request("POST", path, {"repair_id": "fix-idem", "k": 1})
    expect(status == 200, f"replay returned {status}")
    expect(replay["replayed"] is True, "replay must be flagged")
    expect(replay["repair_number"] == first["repair_number"],
           "replay must return the original repair number")

    status, changed_k = request(
        "POST", path, {"repair_id": "fix-idem", "k": 2})
    expect(status == 409, f"changed k returned {status}, expected 409")
    expect(changed_k["error"] == "repair_id_conflict",
           f"unexpected error {changed_k}")
    expect(changed_k["existing_repair_number"] == first["repair_number"],
           "409 must reference the original repair")

    # source audit lists only the one repair
    status, fetched = request("GET", f"/audits/{audit['audit_id']}")
    expect(fetched["repairs"] == [first["repair_number"]],
           f"repair listing after conflict: {fetched.get('repairs')}")
    expect(fetched["status"] == "unsatisfiable",
           "failed/conflicting repairs must not rewrite the source audit")


def smoke_repair_guards():
    # repairs only apply to unsatisfiable audits
    status, unique = request("POST", "/audits",
                             {**UNIQUE_PAYLOAD, "request_id": "smoke-repair-guard"})
    expect(status == 201, f"create returned {status}")
    status, body = request(
        "POST", f"/audits/{unique['audit_id']}/repairs",
        {"repair_id": "fix-guard", "k": 1})
    expect(status == 409, f"repair on unique audit returned {status}")
    expect(body["error"] == "audit_not_unsatisfiable", f"body: {body}")

    # k above M//2 is rejected and claims no repair id
    status, audit = request("POST", "/audits",
                            {**REPAIR_UNSAT_PAYLOAD,
                             "request_id": "smoke-repair-guard-k"})
    expect(status == 201, f"create returned {status}")
    status, body = request(
        "POST", f"/audits/{audit['audit_id']}/repairs",
        {"repair_id": "fix-guard-k", "k": 51})
    expect(status == 400, f"k=51 returned {status}")
    expect(body["error"] == "invalid_repair", f"body: {body}")
    expect(body["problems"], "400 must explain the rejected k")

    status, missing = request("POST", "/audits/AUD-999999/repairs",
                              {"repair_id": "fix-x", "k": 1})
    expect(status == 404, f"unknown audit returned {missing}")


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
        ("http: cross-wrap counter repair", smoke_repair_cross_wrap),
        ("http: global-optimum tie verdict", smoke_repair_tie),
        ("http: in-budget exhaustion evidence", smoke_repair_exhausted),
        ("http: repair replay and conflicts", smoke_repair_idempotency),
        ("http: repair guards source audit", smoke_repair_guards),
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
