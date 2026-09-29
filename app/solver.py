"""Unwrap modular device counters onto one absolute timeline.

Each event reports a counter reading in ``[0, M)``; its absolute tick is
``counter + M * wrap`` with an integer wrap count ``>= 0`` (an event cannot
precede absolute zero).  A constraint pins the absolute difference between
two nodes to a closed interval ``[lo, hi]``.  Subtracting the counter
residues turns every constraint into integer difference bounds on the wrap
counts::

    lo <= (c_t + M*k_t) - (c_s + M*k_s) <= hi
    =>  k_t - k_s in [ceil((lo - (c_t - c_s)) / M), floor((hi - (c_t - c_s)) / M)]

The resulting system of integer difference constraints is solved with
shortest-path machinery:

* feasibility is decided by negative-cycle detection, and the witness cycle
  is reported as a recomputable conflict chain;
* per-node wrap ranges come from all-pairs tightest bounds (Floyd-Warshall),
  which also decides unique vs. ambiguous;
* the first two canonical timelines are the lexicographically smallest wrap
  assignments in event-identifier order;
* the first unstable precedence relation is the first pair of nodes (in
  identifier order) whose mutual order is not invariant over all solutions.
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_EVENTS = 12
ZERO = 0  # graph index of the absolute-zero reference node


class InputError(Exception):
    """The request payload violates the input contract."""

    def __init__(self, problems):
        self.problems = [str(p) for p in problems]
        super().__init__("; ".join(self.problems))


@dataclass(frozen=True)
class Event:
    id: str
    counter: int


@dataclass(frozen=True)
class Constraint:
    id: str
    source: str
    target: str
    lo: int
    hi: int


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_id(value):
    return isinstance(value, str) and bool(value.strip())


def _ceil_div(a, b):
    """ceil(a / b) for b > 0, exact for negative a as well."""
    if b <= 0:
        raise ValueError("divisor must be positive")
    return -((-a) // b)


def _disconnected_events(anchor_id, events, constraints):
    parent = {nid: nid for nid in [anchor_id] + [e.id for e in events]}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for c in constraints:
        if c.source in parent and c.target in parent:
            parent[find(c.source)] = find(c.target)
    root = find(anchor_id)
    return [e.id for e in events if find(e.id) != root]


def normalize(payload):
    """Validate the raw payload and return a canonical, solver-ready form.

    Raises InputError listing every problem found.  The returned mapping has
    events and constraints sorted by identifier so solving is deterministic.
    """
    if not isinstance(payload, dict):
        raise InputError(["payload must be a JSON object"])
    problems = []

    allowed = {"request_id", "modulus", "anchor", "events", "constraints"}
    for key in sorted(payload):
        if key not in allowed:
            problems.append(f"unknown field '{key}'")

    request_id = payload.get("request_id")
    if not _is_id(request_id):
        problems.append("request_id must be a non-empty string")

    modulus = payload.get("modulus")
    modulus_ok = _is_int(modulus) and modulus >= 2
    if not modulus_ok:
        problems.append("modulus must be an integer >= 2")

    anchor = payload.get("anchor")
    anchor_id = None
    anchor_abs = None
    if not isinstance(anchor, dict):
        problems.append("anchor must be an object {id, absolute}")
    else:
        for key in sorted(anchor):
            if key not in {"id", "absolute"}:
                problems.append(f"unknown anchor field '{key}'")
        anchor_id = anchor.get("id")
        if not _is_id(anchor_id):
            problems.append("anchor.id must be a non-empty string")
            anchor_id = None
        anchor_abs = anchor.get("absolute")
        if not _is_int(anchor_abs) or anchor_abs < 0:
            problems.append("anchor.absolute must be an integer >= 0")
            anchor_abs = None

    events = []
    raw_events = payload.get("events")
    if not isinstance(raw_events, list):
        problems.append("events must be a list (possibly empty)")
    else:
        if len(raw_events) > MAX_EVENTS:
            problems.append(
                f"at most {MAX_EVENTS} events allowed, got {len(raw_events)}")
        seen = set()
        for pos, item in enumerate(raw_events):
            if not isinstance(item, dict):
                problems.append(f"events[{pos}] must be an object {{id, counter}}")
                continue
            for key in sorted(item):
                if key not in {"id", "counter"}:
                    problems.append(f"unknown events[{pos}] field '{key}'")
            eid = item.get("id")
            counter = item.get("counter")
            if not _is_id(eid):
                problems.append(f"events[{pos}].id must be a non-empty string")
                continue
            if eid in seen:
                problems.append(f"duplicate event id '{eid}'")
                continue
            seen.add(eid)
            if not _is_int(counter):
                problems.append(f"event '{eid}' counter must be an integer")
                continue
            if modulus_ok and not 0 <= counter < modulus:
                problems.append(
                    f"event '{eid}' counter {counter} outside [0, {modulus})")
                continue
            events.append(Event(eid, counter))
        if anchor_id is not None and anchor_id in {e.id for e in events}:
            problems.append(f"event id '{anchor_id}' collides with the anchor id")

    constraints = []
    raw_constraints = payload.get("constraints")
    if not isinstance(raw_constraints, list):
        problems.append("constraints must be a list (possibly empty)")
    else:
        node_ids = ({anchor_id} if anchor_id else set()) | {e.id for e in events}
        seen_ids = set()
        for pos, item in enumerate(raw_constraints):
            if not isinstance(item, dict):
                problems.append(
                    f"constraints[{pos}] must be an object "
                    "{id, source, target, lo, hi}")
                continue
            for key in sorted(item):
                if key not in {"id", "source", "target", "lo", "hi"}:
                    problems.append(f"unknown constraints[{pos}] field '{key}'")
            cid = item.get("id")
            if not _is_id(cid):
                problems.append(f"constraints[{pos}].id must be a non-empty string")
                continue
            if cid in seen_ids:
                problems.append(f"duplicate constraint id '{cid}'")
                continue
            seen_ids.add(cid)
            source = item.get("source")
            target = item.get("target")
            lo = item.get("lo")
            hi = item.get("hi")
            ok = True
            if source not in node_ids:
                problems.append(f"constraint '{cid}' source '{source}' is not a known node")
                ok = False
            if target not in node_ids:
                problems.append(f"constraint '{cid}' target '{target}' is not a known node")
                ok = False
            if not _is_int(lo) or not _is_int(hi):
                problems.append(f"constraint '{cid}' lo/hi must be integers")
                ok = False
            elif lo > hi:
                problems.append(f"constraint '{cid}' interval [{lo}, {hi}] is empty")
                ok = False
            if ok:
                constraints.append(Constraint(cid, source, target, lo, hi))

    if not problems and anchor_id is not None:
        for eid in _disconnected_events(anchor_id, events, constraints):
            problems.append(
                f"event '{eid}' is not connected to the anchor by constraints")

    if problems:
        raise InputError(problems)

    return {
        "request_id": request_id,
        "modulus": modulus,
        "anchor": {"id": anchor_id, "absolute": anchor_abs},
        "events": sorted(events, key=lambda e: e.id),
        "constraints": sorted(constraints, key=lambda c: c.id),
    }


# ---------------------------------------------------------------------------
# difference-constraint machinery
# ---------------------------------------------------------------------------

def _floyd_warshall(n, edges):
    """All-pairs shortest paths; ``dist[i][i] < 0`` marks a negative cycle."""
    dist = [[None] * n for _ in range(n)]
    for i in range(n):
        dist[i][i] = 0
    for u, v, w, _meta in edges:
        if dist[u][v] is None or w < dist[u][v]:
            dist[u][v] = w
    for k in range(n):
        dk = dist[k]
        for i in range(n):
            dik = dist[i][k]
            if dik is None:
                continue
            di = dist[i]
            for j in range(n):
                dkj = dk[j]
                if dkj is None:
                    continue
                cand = dik + dkj
                if di[j] is None or cand < di[j]:
                    di[j] = cand
    return dist


def _infeasible(dist):
    return any(dist[i][i] < 0 for i in range(len(dist)))


def _negative_cycle_edges(n, edges):
    """Return edge indices forming a negative cycle, or None if none exists."""
    dist = [0] * n  # virtual super-source at distance 0 from every node
    parent = [None] * n
    last = None
    for _ in range(n):
        last = None
        for ei, (u, v, w, _meta) in enumerate(edges):
            if dist[u] + w < dist[v]:
                dist[v] = dist[u] + w
                parent[v] = (ei, u)
                last = v
    if last is None:
        return None
    node = last
    for _ in range(n):
        node = parent[node][1]
    cycle = []
    cur = node
    while True:
        ei, u = parent[cur]
        cycle.append(ei)
        cur = u
        if cur == node:
            break
    cycle.reverse()
    return cycle


def _eq_edges(fixed):
    """Edges pinning each node in ``fixed`` to its assigned wrap count."""
    out = []
    for node, value in fixed.items():
        out.append((ZERO, node, value, {"kind": "fix"}))
        out.append((node, ZERO, -value, {"kind": "fix"}))
    return out


def _lex_min_assignment(n, base_edges, order, fixed):
    """Lexicographically smallest wrap assignment over ``order`` given ``fixed``."""
    fixed = dict(fixed)
    for node in order:
        if node in fixed:
            continue
        dist = _floyd_warshall(n, base_edges + _eq_edges(fixed))
        if _infeasible(dist):
            return None
        fixed[node] = -dist[node][ZERO]
    return fixed


def _lex_second_assignment(n, base_edges, order, first):
    """Smallest feasible assignment strictly greater than ``first`` (lex order)."""
    for j in range(len(order) - 1, -1, -1):
        prefix = {order[i]: first[order[i]] for i in range(j)}
        dist = _floyd_warshall(n, base_edges + _eq_edges(prefix))
        node = order[j]
        if first[node] + 1 <= dist[ZERO][node]:
            prefix[node] = first[node] + 1
            return _lex_min_assignment(n, base_edges, order, prefix)
    return None


# ---------------------------------------------------------------------------
# reporting helpers
# ---------------------------------------------------------------------------

def _step_description(u, v, w, meta, labels):
    edge = f"{labels[u]}->{labels[v]}"
    kind = meta["kind"]
    if kind == "constraint_upper":
        return {
            "edge": edge,
            "constraint": meta["constraint"],
            "kind": "upper_bound",
            "relation": f"wrap({meta['target']}) - wrap({meta['source']}) <= {meta['bound']}",
            "weight": w,
        }
    if kind == "constraint_lower":
        return {
            "edge": edge,
            "constraint": meta["constraint"],
            "kind": "lower_bound",
            "relation": f"wrap({meta['target']}) - wrap({meta['source']}) >= {meta['bound']}",
            "weight": w,
        }
    if kind == "anchor":
        op = "<=" if meta["side"] == "upper" else ">="
        return {
            "edge": edge,
            "kind": "anchor",
            "relation": f"wrap({meta['node']}) {op} {meta['wrap']}",
            "weight": w,
        }
    return {
        "edge": edge,
        "kind": "non_negative",
        "relation": f"wrap({meta['node']}) >= 0",
        "weight": w,
    }


def _conflict_chain(n, edges, labels):
    cycle = _negative_cycle_edges(n, edges)
    if cycle is None:
        return {
            "constraints": [],
            "cycle": [],
            "steps": [],
            "total_weight": None,
            "explanation": "infeasible system, but no witness cycle could be extracted",
        }
    steps = []
    nodes = []
    total = 0
    for pos, ei in enumerate(cycle):
        u, v, w, meta = edges[ei]
        total += w
        if pos == 0:
            nodes.append(labels[u])
        nodes.append(labels[v])
        steps.append(_step_description(u, v, w, meta, labels))
    constraint_ids = sorted({s["constraint"] for s in steps if "constraint" in s})
    explanation = (
        f"walking the cycle {' -> '.join(nodes)} forces a wrap count below "
        f"itself: the bounds sum to {total} < 0, so no integer assignment exists"
    )
    return {
        "constraints": constraint_ids,
        "cycle": nodes,
        "steps": steps,
        "total_weight": total,
        "explanation": explanation,
    }


def _timeline(node_ids, counters, modulus, anchor_id, wraps):
    return [
        {
            "id": nid,
            "anchor": nid == anchor_id,
            "counter": counters[nid],
            "wrap": wraps[nid],
            "absolute": counters[nid] + modulus * wraps[nid],
        }
        for nid in node_ids
    ]


def _relation_of(delta):
    if delta < 0:
        return "before"
    if delta > 0:
        return "after"
    return "same"


def _first_unstable_relation(node_ids, idx, counters, modulus, dist, wraps1, wraps2):
    """First pair (identifier order) whose precedence varies across solutions."""
    for x in range(len(node_ids)):
        for y in range(x + 1, len(node_ids)):
            nx, ny = node_ids[x], node_ids[y]
            i, j = idx[nx], idx[ny]
            delta_c = counters[nx] - counters[ny]
            d_min = delta_c + modulus * (-dist[i][j])
            d_max = delta_c + modulus * dist[j][i]
            possible = []
            if d_min < 0:
                possible.append("before")
            if d_min <= 0 <= d_max and (0 - d_min) % modulus == 0:
                possible.append("same")
            if d_max > 0:
                possible.append("after")
            if len(possible) <= 1:
                continue
            rel1 = _relation_of(
                (counters[nx] + modulus * wraps1[nx])
                - (counters[ny] + modulus * wraps1[ny]))
            rel2 = _relation_of(
                (counters[nx] + modulus * wraps2[nx])
                - (counters[ny] + modulus * wraps2[ny]))
            return {
                "events": [nx, ny],
                "min_delta": d_min,
                "max_delta": d_max,
                "possible_relations": possible,
                "in_timeline_1": rel1,
                "in_timeline_2": rel2,
            }
    return None


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------

def solve(norm):
    """Solve a normalized request; returns {status, conclusion, evidence}."""
    modulus = norm["modulus"]
    anchor = norm["anchor"]
    events = norm["events"]
    constraints = norm["constraints"]

    anchor_id = anchor["id"]
    node_ids = [anchor_id] + [e.id for e in events]
    idx = {nid: i + 1 for i, nid in enumerate(node_ids)}
    labels = ["ZERO"] + node_ids
    n = len(labels)

    counters = {anchor_id: anchor["absolute"] % modulus}
    for e in events:
        counters[e.id] = e.counter
    anchor_wrap = anchor["absolute"] // modulus

    edges = []

    def add(u, v, w, meta):
        edges.append((u, v, w, meta))

    # Pin the anchor wrap count to the known absolute tick.
    a = idx[anchor_id]
    add(ZERO, a, anchor_wrap,
        {"kind": "anchor", "node": anchor_id, "side": "upper", "wrap": anchor_wrap})
    add(a, ZERO, -anchor_wrap,
        {"kind": "anchor", "node": anchor_id, "side": "lower", "wrap": anchor_wrap})

    # Wrap counts are non-negative: nothing happens before absolute zero.
    for e in events:
        add(idx[e.id], ZERO, 0, {"kind": "non_negative", "node": e.id})

    derivation = []
    for c in constraints:
        s, t = idx[c.source], idx[c.target]
        delta_c = counters[c.target] - counters[c.source]
        lo_k = _ceil_div(c.lo - delta_c, modulus)
        hi_k = (c.hi - delta_c) // modulus
        add(s, t, hi_k, {
            "kind": "constraint_upper", "constraint": c.id,
            "source": c.source, "target": c.target, "bound": hi_k})
        add(t, s, -lo_k, {
            "kind": "constraint_lower", "constraint": c.id,
            "source": c.source, "target": c.target, "bound": lo_k})
        derivation.append({
            "constraint": c.id,
            "source": c.source,
            "target": c.target,
            "interval": [c.lo, c.hi],
            "counter_delta": delta_c,
            "wrap_lower": lo_k,
            "wrap_upper": hi_k,
            "relation": f"wrap({c.target}) - wrap({c.source}) in [{lo_k}, {hi_k}]",
        })

    dist = _floyd_warshall(n, edges)
    if _infeasible(dist):
        return {
            "status": "unsatisfiable",
            "conclusion": {"conflict_chain": _conflict_chain(n, edges, labels)},
            "evidence": {"derivation": derivation},
        }

    node_ranges = []
    for nid in node_ids:
        i = idx[nid]
        lo_k = -dist[i][ZERO]
        hi_k = dist[ZERO][i]
        node_ranges.append({
            "id": nid,
            "min_wrap": lo_k,
            "max_wrap": hi_k,
            "min_absolute": counters[nid] + modulus * lo_k,
            "max_absolute": counters[nid] + modulus * hi_k,
        })

    evidence = {
        "derivation": derivation,
        "node_ranges": node_ranges,
        "anchor": {"id": anchor_id, "absolute": anchor["absolute"], "wrap": anchor_wrap},
    }

    if all(r["min_wrap"] == r["max_wrap"] for r in node_ranges):
        wraps = {nid: anchor_wrap for nid in [anchor_id]}
        wraps.update({r["id"]: r["min_wrap"] for r in node_ranges})
        return {
            "status": "unique",
            "conclusion": {
                "timeline": _timeline(node_ids, counters, modulus, anchor_id, wraps),
                "summary": "every event collapses to a single absolute tick",
            },
            "evidence": evidence,
        }

    order = [idx[e.id] for e in events]  # event identifiers, sorted
    first = _lex_min_assignment(n, edges, order, {})
    second = _lex_second_assignment(n, edges, order, first)
    wraps1 = {anchor_id: anchor_wrap}
    wraps1.update({e.id: first[idx[e.id]] for e in events})
    wraps2 = {anchor_id: anchor_wrap}
    wraps2.update({e.id: second[idx[e.id]] for e in events})

    return {
        "status": "ambiguous",
        "conclusion": {
            "timelines": [
                _timeline(node_ids, counters, modulus, anchor_id, wraps1),
                _timeline(node_ids, counters, modulus, anchor_id, wraps2),
            ],
            "first_unstable_relation": _first_unstable_relation(
                node_ids, idx, counters, modulus, dist, wraps1, wraps2),
            "summary": "multiple absolute timelines satisfy every constraint",
        },
        "evidence": evidence,
    }
