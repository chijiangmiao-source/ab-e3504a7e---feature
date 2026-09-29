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
