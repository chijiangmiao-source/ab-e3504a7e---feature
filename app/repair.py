"""Optimal counter repairs for an otherwise unsatisfiable wrap audit.

An auditor may only react to a frozen ``unsatisfiable`` audit by correcting
device *counter readings* -- never by widening the causal windows.  Each event
tick may be shifted by an integer ``d`` in ``[-K, K]`` with ``K <= M // 2``;
the corrected reading is re-normalised modulo ``M`` and the *original* wrap
difference constraints must then hold exactly.  Writing the normalised
residue ``r_i = (c_i + d_i) mod M``::

    lo_k(d) = ceil((lo - (r_t - r_s)) / M)
    hi_k(d) = floor((hi - (r_t - r_s)) / M)

The correction vector and the integer wrap constraints are searched
*together* -- never by flat-enumerating all vectors, never by greedily
"fixing" readings one at a time:

1. a quick MRV feasibility pass (values 0, +/-1, +/-2, ...) obtains an
   incumbent used only as an upper bound;
2. the authoritative search is a depth-first enumeration in event-identifier
   order, ticks ascending from ``-K`` to ``K``;
3. every node maintains a tick domain per undecided event.  A value is
   removed when pinning the event to it while every *other* undecided event
   is relaxed over its current domain already empties a wrap interval or
   closes a negative conflict cycle.  Revision iterates to a fixpoint, so a
   conflict chain on one event propagates into the others' domains;
4. surviving domains drive conflict-chain lower bounds: an event whose
   domain no longer contains zero must change, and its smallest surviving
   ``|tick|`` lower-bounds the absolute-tick sum.  Subtrees that cannot beat
   the incumbent are cut before they are expanded;
5. every removed tick carries the recomputable witness that killed it; the
   full search tree is returned either as optimality evidence or, when every
   branch dies within budget, as exhaustion proof.

The objective is strictly ordered: (1) number of changed events, (2) sum of
absolute correction ticks, (3) lexicographic tie-break over the correction
vector in event-identifier order.  All arithmetic is exact integer
arithmetic.
"""

from __future__ import annotations

from .solver import (
    Event,
    InputError,
    _ceil_div,
    _negative_cycle_edges,
    solve,
)


# ---------------------------------------------------------------------------
# request validation
# ---------------------------------------------------------------------------

def validate_repair_request(body, modulus):
    """Validate ``{fix_id, K}`` against the frozen source audit."""
    if not isinstance(body, dict):
        raise InputError(["repair body must be a JSON object {fix_id, K}"])
    problems = []
    for key in sorted(body):
        if key not in {"fix_id", "K"}:
            problems.append(f"unknown field '{key}'")
    fix_id = body.get("fix_id")
    if not isinstance(fix_id, str) or not fix_id.strip():
        problems.append("fix_id must be a non-empty string")
    limit = modulus // 2
    K = body.get("K")
    if not isinstance(K, int) or isinstance(K, bool):
        problems.append("K must be an integer")
    elif not 0 <= K <= limit:
        problems.append(
            f"K must be an integer in [0, {limit}] (at most half the modulus "
            f"{modulus})")
    if problems:
        raise InputError(problems)
    return {"fix_id": fix_id, "K": K}


# ---------------------------------------------------------------------------
# parameterised difference constraints
# ---------------------------------------------------------------------------

def _build_edges(norm, domains):
    """Difference-constraint edges with corrections relaxed over ``domains``.

    ``domains`` maps graph index (anchor = 1) to a sorted list of surviving
    ticks; the anchor domain is ``[0]``.  Per constraint the correction delta
    ranges over ``[min D_t - max D_s, max D_t - min D_s]``; taking the union
    of the resulting wrap bounds yields a single integer interval, so the
    edge set is a valid relaxation of every completion of the domains.

    Returns ``(n, labels, edges, gaps)``; ``gaps`` lists constraints whose
    wrap interval is empty for every correction-delta value.
    """
    M = norm["modulus"]
    anchor_id = norm["anchor"]["id"]
    labels = ["ZERO", anchor_id] + [e.id for e in norm["events"]]
    n = len(labels)
    idx = {nid: i for i, nid in enumerate(labels)}
    a = idx[anchor_id]
    anchor_wrap = norm["anchor"]["absolute"] // M
    counters = {anchor_id: norm["anchor"]["absolute"] % M}
    for e in norm["events"]:
        counters[e.id] = e.counter

    edges = []
    edges.append((0, a, anchor_wrap,
                  {"kind": "anchor", "node": anchor_id, "side": "upper",
                   "wrap": anchor_wrap}))
    edges.append((a, 0, -anchor_wrap,
                  {"kind": "anchor", "node": anchor_id, "side": "lower",
                   "wrap": anchor_wrap}))
    for e in norm["events"]:
        edges.append((idx[e.id], 0, 0,
                      {"kind": "non_negative", "node": e.id}))

    gaps = []
    # Normalised residue range per node: corrections are applied to the
    # reading and THEN re-normalised modulo M, so a tick can carry the
    # residue across 0 or M and shift the wrap bound by one.
    rlo, rhi = {a: counters[anchor_id]}, {a: counters[anchor_id]}
    for e in norm["events"]:
        gi = idx[e.id]
        residues = [(e.counter + d) % M for d in domains[gi]]
        rlo[gi], rhi[gi] = min(residues), max(residues)
    for c in norm["constraints"]:
        s, t = idx[c.source], idx[c.target]
        # Range of the *normalised* residue difference r_target - r_source;
        # wrap bounds follow directly from lo <= dr + M*(k_t-k_s) <= hi.
        Lr = rlo[t] - rhi[s]
        Ur = rhi[t] - rlo[s]
        # Raw correction-tick range (same data, shown for recomputation).
        tick_lo = domains[t][0] - domains[s][-1]
        tick_hi = domains[t][-1] - domains[s][0]
        delta_c = counters[c.target] - counters[c.source]
        lo_k = _ceil_div(c.lo - Ur, M)
        hi_k = (c.hi - Lr) // M
        pinned = len(domains[s]) == 1 and len(domains[t]) == 1
        base = {
            "constraint": c.id, "source": c.source, "target": c.target,
            "interval": [c.lo, c.hi], "counter_delta": delta_c,
            "correction_tick_range": [tick_lo, tick_hi],
            "residue_delta_range": [Lr, Ur],
            "wrap_lower": lo_k, "wrap_upper": hi_k, "pinned": pinned,
        }
        if lo_k > hi_k:
            gaps.append(base)
            continue
        edges.append((s, t, hi_k, {
            **base, "kind": "constraint_upper",
            "relation": f"wrap({c.target}) - wrap({c.source}) <= {hi_k}"}))
        edges.append((t, s, -lo_k, {
            **base, "kind": "constraint_lower",
            "relation": f"wrap({c.target}) - wrap({c.source}) >= {lo_k}"}))
    return n, labels, edges, gaps


def _cycle_witness(labels, edges, n):
    cycle = _negative_cycle_edges(n, edges)
    if cycle is None:
        return None
    steps = []
    nodes = []
    total = 0
    for pos, ei in enumerate(cycle):
        u, v, w, meta = edges[ei]
        total += w
        if pos == 0:
            nodes.append(labels[u])
        nodes.append(labels[v])
        steps.append({
            "edge": f"{labels[u]}->{labels[v]}", "weight": w,
            "kind": meta["kind"],
            **({"constraint": meta["constraint"],
                "relation": meta["relation"]}
               if "constraint" in meta else {}),
            **({"node": meta["node"]} if "node" in meta else {}),
            "residue_delta_range": meta.get("residue_delta_range"),
            "correction_tick_range": meta.get("correction_tick_range"),
            "pinned": meta.get("pinned"),
        })
    return {
        "kind": "negative_cycle",
        "constraints": sorted({s["constraint"] for s in steps
                               if "constraint" in s}),
        "cycle": nodes,
        "steps": steps,
        "total_weight": total,
        "explanation": (
            f"the relaxed bounds around {' -> '.join(nodes)} sum to "
            f"{total} < 0; no completion of these tick domains exists"),
    }


# ---------------------------------------------------------------------------
# joint search
# ---------------------------------------------------------------------------

def plan_repair(norm, K):
    """Search the globally optimal correction vector.

    Returns a ``repaired`` plan or a ``beyond_budget`` report carrying the
    full pruned branch tree with per-tick recomputable witnesses.
    """
    events = norm["events"]  # sorted by identifier
    gis = [i + 2 for i in range(len(events))]  # graph indices (1 = anchor)
    name = {gi: events[gi - 2].id for gi in gis}

    stats = {
        "phase1_nodes": 0,
        "nodes_visited": 0,
        "domain_values_removed": 0,
        "objective_prunes": 0,
        "feasible_leaves": 0,
        "infeasible_leaves": 0,
        "infeasible_prefixes": 0,
        "witness_checks": 0,
        "witness_cache_hits": 0,
    }
    witness_cache = {}

    def witness(domains):
        """None if a completion might exist, otherwise a recomputable proof."""
        key = tuple(sorted((gi, tuple(vals)) for gi, vals in domains.items()))
        stats["witness_checks"] += 1
        if key in witness_cache:
            stats["witness_cache_hits"] += 1
            return witness_cache[key]
        n, labels, edges, gaps = _build_edges(norm, domains)
        out = None
        if gaps:
            g = gaps[0]
            out = {
                "kind": "empty_wrap_interval",
                **{k: g[k] for k in (
                    "constraint", "source", "target", "interval",
                    "counter_delta", "correction_tick_range",
                    "residue_delta_range",
                    "wrap_lower", "wrap_upper", "pinned")},
                "explanation": (
                    f"constraint {g['constraint']} needs wrap({g['target']}) "
                    f"- wrap({g['source']}) in [{g['wrap_lower']}, "
                    f"{g['wrap_upper']}] for normalised residue delta in "
                    f"{g['residue_delta_range']} (correction ticks "
                    f"{g['correction_tick_range']}), an empty integer "
                    "interval"),
            }
        else:
            out = _cycle_witness(labels, edges, n)
        witness_cache[key] = out
        return out

    def revise(domains):
        """Remove tick values already refuted by conflict chains.

        Iterates to a fixpoint: pinning one event shrinks the correction
        ranges of its incident constraints, which can refute values on other
        events.  Returns ``(surviving, removed, emptied)`` with ``removed``
        mapping graph index to ``{tick: witness}``.
        """
        work = {gi: list(vals) for gi, vals in domains.items()}
        removed = {}
        while True:
            changed_any = False
            for gi in gis:
                vals = work[gi]
                if len(vals) <= 1:
                    continue
                keep = []
                for d in vals:
                    trial = dict(work)
                    trial[gi] = [d]
                    proof = witness(trial)
                    if proof is None:
                        keep.append(d)
                    else:
                        removed.setdefault(gi, {})[d] = proof
                        stats["domain_values_removed"] += 1
                        changed_any = True
                work[gi] = keep
                if not keep:
                    return work, removed, gi
            if not changed_any:
                return work, removed, None

    def full_domains():
        return {1: [0], **{gi: list(range(-K, K + 1)) for gi in gis}}

    def objective_of(assign):
        return (sum(1 for d in assign.values() if d != 0),
                sum(abs(d) for d in assign.values()))

    # -- phase 1: any feasible leaf, MRV + 0,+/-1,... value order ----------
    def find_feasible(domains, assign):
        stats["phase1_nodes"] += 1
        proof = witness(domains)
        if proof is not None:
            return None
        free = [gi for gi in gis if gi not in assign]
        if not free:
            return dict(assign)
        gi = min(free, key=lambda x: len(domains[x]))
        vals = sorted(domains[gi], key=lambda d: (abs(d), d))
        for d in vals:
            child = dict(domains)
            child[gi] = [d]
            assign[gi] = d
            found = find_feasible(child, assign)
            if found is not None:
                return found
            del assign[gi]
        return None

    seed = find_feasible(full_domains(), {})
    incumbent = None
    if seed is not None:
        ch, tc = objective_of(seed)
        incumbent = (ch, tc, tuple(seed[gi] for gi in gis))

    # -- phase 2: exhaustive, identifier order, ticks -K..K ---------------
    incumbent_ref = [incumbent]  # replaced whenever a better leaf is found

    def lower_bound(surviving, pos, changed, cost):
        remaining = gis[pos:]
        forced_events = [name[gi] for gi in remaining if 0 not in surviving[gi]]
        min_ticks = {
            name[gi]: (0 if 0 in surviving[gi]
                       else min(abs(d) for d in surviving[gi]))
            for gi in remaining}
        return (
            changed + len(forced_events),
            cost + sum(min_ticks.values()),
            {"forced_events": forced_events, "min_ticks": min_ticks},
        )

    def dfs(pos, domains, changed, cost, prefix, node):
        stats["nodes_visited"] += 1
        node["event"] = None if pos == 0 else name[gis[pos - 1]]
        node["tick"] = None if pos == 0 else prefix[-1]
        node["changed_so_far"] = changed
        node["cost_so_far"] = cost
        node["prefix_vector"] = list(prefix)

        # cheap bound from the parent's surviving domains before revising
        lb_ch, lb_cost, lb_info = lower_bound(domains, pos, changed, cost)
        seed_inc = incumbent_ref[0]
        if seed_inc is not None and (
                lb_ch > seed_inc[0]
                or (lb_ch == seed_inc[0] and lb_cost > seed_inc[1])):
            node["outcome"] = "pruned_objective"
            node["lower_bound"] = {"changed_events": lb_ch,
                                   "abs_tick_sum": lb_cost, **lb_info}
            node["incumbent"] = {"changed_events": seed_inc[0],
                                 "abs_tick_sum": seed_inc[1]}
            stats["objective_prunes"] += 1
            return

        surviving, removed, emptied = revise(domains)
        if removed:
            node["domain_pruning"] = [
                {"event": name[gi],
                 "removed_ticks": sorted(ticks),
                 "witnesses": [ticks[d] for d in sorted(ticks)]}
                for gi, ticks in sorted(removed.items())]
        if emptied is not None:
            node["outcome"] = ("infeasible_leaf" if pos == len(events)
                               else "infeasible_prefix")
            node["witness"] = removed[emptied][
                sorted(removed[emptied])[0]]
            stats[("infeasible_leaves" if pos == len(events)
                   else "infeasible_prefixes")] += 1
            return

        if pos == len(events):
            proof = witness(surviving)
            if proof is not None:
                node["outcome"] = "infeasible_leaf"
                node["witness"] = proof
                stats["infeasible_leaves"] += 1
                return
            vector = tuple(surviving[gi][0] for gi in gis)
            candidate = (changed, cost, vector)
            node["outcome"] = "feasible_leaf"
            node["objective"] = {"changed_events": changed,
                                 "abs_tick_sum": cost}
            stats["feasible_leaves"] += 1
            cur = incumbent_ref[0]
            if cur is None or candidate < cur:
                incumbent_ref[0] = candidate
            return

        lb_ch, lb_cost, lb_info = lower_bound(surviving, pos, changed, cost)
        node["lower_bound"] = {"changed_events": lb_ch,
                               "abs_tick_sum": lb_cost, **lb_info}
        cur = incumbent_ref[0]
        if cur is not None and (
                lb_ch > cur[0]
                or (lb_ch == cur[0] and lb_cost > cur[1])):
            node["outcome"] = "pruned_objective"
            node["incumbent"] = {"changed_events": cur[0],
                                 "abs_tick_sum": cur[1]}
            stats["objective_prunes"] += 1
            return

        gi = gis[pos]
        allowed = set(surviving[gi])
        children = []
        for d in range(-K, K + 1):
            child = {"event": name[gi], "tick": d}
            children.append(child)
            if d not in allowed:
                child["outcome"] = "pruned_domain"
                child["witness"] = removed.get(gi, {}).get(d)
                continue
            child_domains = dict(surviving)
            child_domains[gi] = [d]
            dfs(pos + 1, child_domains,
                changed + (1 if d != 0 else 0), cost + abs(d),
                prefix + [d], child)
        node["children"] = children

    root = {}
    dfs(0, full_domains(), 0, 0, [], root)

    search = {
        "budget_K": K,
        "order": [e.id for e in events],
        "tick_domain": [-K, K],
        "initial_incumbent": (
            None if incumbent is None or incumbent_ref[0] is None
            else {"changed_events": incumbent[0],
                  "abs_tick_sum": incumbent[1],
                  "vector": list(incumbent[2]),
                  "note": "feasibility-seed upper bound only; the "
                          "exhaustive phase proves the global optimum"}),
        "stats": stats,
        "tree": root,
        "method": (
            "phase 1 seeds an incumbent via MRV feasibility search; phase 2 "
            "is an exhaustive depth-first enumeration over corrections and "
            "wrap difference constraints jointly -- per-event tick domains "
            "are revised to a fixpoint against relaxed-prefix empty intervals "
            "and negative conflict cycles, surviving domains drive "
            "changed-event and absolute-tick lower bounds, ticks are tried "
            "from -K to K in event-identifier order, so the surviving best "
            "leaf is the lexicographically smallest correction vector among "
            "all global optima"),
    }

    winner = incumbent_ref[0]
    if winner is None:
        return {
            "status": "beyond_budget",
            "search": search,
            "summary": (
                f"every branch within |d| <= {K} was cut by an empty wrap "
                "interval or a negative conflict cycle; no correction exists "
                "without touching the causal windows"),
        }

    changed, total_cost, vector = winner
    return _build_success(norm, K, vector, changed, total_cost, search)


# ---------------------------------------------------------------------------
# success report
# ---------------------------------------------------------------------------

def _corrected_norm(norm, vector):
    M = norm["modulus"]
    by_id = {e.id: vector[i] for i, e in enumerate(norm["events"])}
    corrected_events = [
        Event(e.id, (e.counter + by_id[e.id]) % M) for e in norm["events"]]
    return {
        "request_id": norm["request_id"],
        "modulus": M,
        "anchor": dict(norm["anchor"]),
        "events": sorted(corrected_events, key=lambda e: e.id),
        "constraints": list(norm["constraints"]),
    }, by_id


def _build_success(norm, K, vector, changed, total_cost, search):
    M = norm["modulus"]
    corrected, by_id = _corrected_norm(norm, vector)
    resolved = solve(corrected)
    if resolved["status"] == "unique":
        timeline = resolved["conclusion"]["timeline"]
        repaired_status = "unique"
    elif resolved["status"] == "ambiguous":
        # canonical repaired timeline = lexicographically smallest one
        timeline = resolved["conclusion"]["timelines"][0]
        repaired_status = "ambiguous"
    else:
        raise AssertionError("repair branch produced a feasible leaf but the "
                             "corrected audit is still unsatisfiable")
    absolute = {row["id"]: row["absolute"] for row in timeline}
    wraps = {row["id"]: row["wrap"] for row in timeline}

    anchor_id = norm["anchor"]["id"]
    counters = {anchor_id: norm["anchor"]["absolute"] % M}
    counters.update({e.id: e.counter for e in norm["events"]})

    corrections = []
    for i, e in enumerate(norm["events"]):
        d = vector[i]
        corrections.append({
            "id": e.id,
            "original_counter": e.counter,
            "tick_adjustment": d,
            "changed": d != 0,
            "corrected_counter": (e.counter + d) % M,
            "wrap": wraps[e.id],
            "absolute": absolute[e.id],
        })

    deltas = []
    anchor_residue = norm["anchor"]["absolute"] % M
    for c in norm["constraints"]:
        ds = 0 if c.source == anchor_id else by_id[c.source]
        dt = 0 if c.target == anchor_id else by_id[c.target]
        cs_raw = (anchor_residue if c.source == anchor_id
                  else counters[c.source]) + ds
        ct_raw = (anchor_residue if c.target == anchor_id
                  else counters[c.target]) + dt
        cs_mod, ct_mod = cs_raw % M, ct_raw % M
        dc_mod = ct_mod - cs_mod
        lo_k = _ceil_div(c.lo - dc_mod, M)
        hi_k = (c.hi - dc_mod) // M
        delta = absolute[c.target] - absolute[c.source]
        deltas.append({
            "constraint": c.id,
            "source": c.source,
            "target": c.target,
            "interval": [c.lo, c.hi],
            "counter_delta": counters[c.target] - counters[c.source],
            "correction_delta": dt - ds,
            "source_residue": cs_mod,
            "target_residue": ct_mod,
            "residue_delta": dc_mod,
            "absolute_delta": delta,
            "wrap_difference": wraps[c.target] - wraps[c.source],
            "wrap_bounds": [lo_k, hi_k],
            "satisfied": c.lo <= delta <= c.hi and lo_k <= hi_k,
        })

    return {
        "status": "repaired",
        "search": search,
        "objective": {
            "changed_events": changed,
            "abs_tick_sum": total_cost,
            "tie_break": "lexicographically smallest vector over event ids",
        },
        "canonical_corrections": corrections,
        "repaired_status": repaired_status,
        "repaired_timeline": timeline,
        "constraint_deltas": deltas,
        "summary": (
            f"{changed} event reading(s) corrected with {total_cost} tick(s) "
            "in total; the original causal windows are satisfied exactly"),
    }
