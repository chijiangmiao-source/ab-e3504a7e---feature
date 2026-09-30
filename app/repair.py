"""Counter-correction search for audits previously concluded ``unsatisfiable``.

The causal window is never widened to hide a collection error.  The frozen
source input is reopened read-only and each *event* counter may be corrected
by an integer tick ``delta in [-K, K]`` (``0 <= K <= M // 2``); the corrected
reading is normalized back into ``[0, M)``::

    counter' = (counter + delta) mod M

The ordinary wrap difference constraints are then solved jointly with the
correction choices:

    lo <= (c'_t + M*k_t) - (c'_s + M*k_s) <= hi
    =>  k_t - k_s in [ceil((lo - (c'_t - c'_s)) / M),
                      floor((hi - (c'_t - c'_s)) / M)]

The optimum is lexicographic on three objectives:

1. minimize the number of changed events (``delta != 0``);
2. minimize the sum of absolute corrections;
3. stabilize ties with the correction vector in event-identifier order
   (lexicographically smallest).

The search never enumerates all correction vectors, never adjusts readings
greedily one by one, and never uses floating point.  It is branch-and-bound:

* an incremental all-pairs shortest-path closure is kept over the nodes whose
  corrections are fixed; a candidate that closes a negative cycle is pruned
  exactly, and on recorded branches the full integer conflict chain is
  extracted with its recomputable negative weight;
* free corrections carry interval domains; an optimistic interval
  difference-constraint closure (every bound taken at its tightest inside the
  domains) gives a necessary-feasibility prune and root domain filtering;
* each remaining variable that is forced off zero by a conflict chain
  contributes a lower bound on both changed events and absolute cost;
* candidates branch in canonical order 0, +1, -1, +2, -2, ... so the first
  feasible vector already supplies strong bounds.

If no feasible correction exists, exhaustion evidence is returned: domains,
counters, prune reasons and sampled conflict branches whose integer bounds
recompute to a negative total.  A deterministic node budget guards the
(arbitrarily large) correction domains: if it is reached, the evidence is
marked ``complete_proof: false``.
"""

from __future__ import annotations

from dataclasses import dataclass

from .solver import (
    Event,
    InputError,
    _ceil_div,
    _conflict_chain,
    _floyd_warshall,
    _negative_cycle_edges,
    solve,
)

# Deterministic branch-and-bound ceiling: reaching it means the search could
# neither prove infeasibility nor certify an optimum, so cut-off exhaustion
# evidence is returned instead of a speculative correction.
SEARCH_BUDGET = 100_000
# Root singleton filtering is fully enumerated only for small domains; with a
# large K the per-node budget bounds the traversal instead.
ENUM_CAP = 61
SAMPLE_LIMIT = 12  # conflict branches captured for recomputation


@dataclass(frozen=True)
class RepairRequest:
    norm: dict
    repair_id: str
    k: int


class _BudgetReached(Exception):
    pass


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def normalize_repair(body, norm):
    """Validate a repair body against the frozen audit ``norm``."""
    if not isinstance(body, dict):
        raise InputError(["repair body must be a JSON object"])
    problems = []
    for key in sorted(body):
        if key not in {"repair_id", "k"}:
            problems.append(f"unknown field '{key}'")

    repair_id = body.get("repair_id")
    if not isinstance(repair_id, str) or not repair_id.strip():
        problems.append("repair_id must be a non-empty string")
        repair_id = None

    k = body.get("k")
    if not (isinstance(k, int) and not isinstance(k, bool)):
        problems.append("k must be an integer")
        k = None
    else:
        cap = norm["modulus"] // 2
        if not 0 <= k <= cap:
            problems.append(
                f"k must be an integer in [0, {cap}] (half of modulus "
                f"{norm['modulus']})")
            k = None

    if problems:
        raise InputError(problems)
    return RepairRequest(norm=norm, repair_id=repair_id, k=k)


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

class _Search:
    def __init__(self, req: RepairRequest):
        self.req = req
        norm = req.norm
        self.modulus = norm["modulus"]
        self.anchor = norm["anchor"]
        self.events = norm["events"]                      # sorted by id
        self.constraints = norm["constraints"]           # sorted by id
        self.m = len(self.events)
        self.k_cap = req.k

        self.anchor_id = self.anchor["id"]
        self.node_ids = [self.anchor_id] + [e.id for e in self.events]
        self.idx = {nid: i + 1 for i, nid in enumerate(self.node_ids)}
        self.labels = ["ZERO"] + self.node_ids
        self.n = len(self.labels)
        self.anchor_wrap = self.anchor["absolute"] // self.modulus
        self.anchor_i = self.idx[self.anchor_id]

        self.counters = {self.anchor_id: self.anchor["absolute"] % self.modulus}
        for e in self.events:
            self.counters[e.id] = e.counter

        # Incident constraints per graph node: (neighbor, constraint,
        # is_source), where is_source says the incident node is c.source.
        self.incident = {i: [] for i in range(self.n)}
        for c in self.constraints:
            s, t = self.idx[c.source], self.idx[c.target]
            self.incident[s].append((t, c, True))
            self.incident[t].append((s, c, False))

        self.stats = {"nodes": 0, "pruned": {}, "deepest": -1}
        self.samples = []
        self.best = None  # (changed, abs_sum, vector)
        self.root_empty = None

    # -- candidates and domains ---------------------------------------------

    def candidates(self, p):
        """Canonical trial order 0, +1, -1, ... lazily, skipping dead values."""
        cap, rem = self.k_cap, self.removed[p]
        if 0 not in rem:
            yield 0
        v = 1
        while v <= cap:
            if v not in rem:
                yield v
            if -v not in rem:
                yield -v
            v += 1

    def filter_domains(self):
        """One round of singleton elimination against full peer domains.

        Value v is dropped when no wrap assignment exists even if every other
        correction is granted its whole [-K, K] domain, so elimination is
        one-sided sound.  Enumerated only for small domains; large K is left
        to branch-and-bound.
        """
        cap = self.k_cap
        self.removed = [set() for _ in range(self.m)]
        self.survivors = [list(range(-cap, cap + 1)) for _ in range(self.m)]
        # Full-domain extrema are needed even while singleton elimination runs.
        self.full_res_min, self.full_res_max = [], []
        for e in self.events:
            lo, hi = self._residue_extremes(
                e.counter, self.modulus, range(-cap, cap + 1))
            self.full_res_min.append(lo)
            self.full_res_max.append(hi)
        if self.m == 0 or 2 * cap + 1 > ENUM_CAP:
            self._cache_residue_extremes()
            return
        for p in range(self.m):
            for v in range(-cap, cap + 1):
                edges = self._optimistic_edges(singleton_p=p, singleton_v=v)
                dist = _floyd_warshall(self.n, edges)
                if any(dist[i][i] < 0 for i in range(self.n)):
                    self.removed[p].add(v)
            if len(self.removed[p]) == 2 * cap + 1:
                self.survivors[p] = []
                self._cache_residue_extremes()
                self.root_empty = p
                return
            self.survivors[p] = [
                v for v in range(-cap, cap + 1) if v not in self.removed[p]]
        self._cache_residue_extremes()

    @staticmethod
    def _residue_extremes(counter, modulus, values):
        """Min/max of ``(counter + v) mod M`` over an iterable of ticks."""
        lo_v, hi_v = counter + min(values), counter + max(values)
        if lo_v < 0 or hi_v >= modulus:
            # the wrapped set reaches both 0 and M-1
            return 0, modulus - 1
        return lo_v, hi_v

    def _cache_residue_extremes(self):
        """Residue extrema of the full and the surviving correction domains."""
        cap = self.k_cap
        self.full_res_min, self.full_res_max = [], []
        self.res_min, self.res_max = [], []
        for p, e in enumerate(self.events):
            lo, hi = self._residue_extremes(
                e.counter, self.modulus, range(-cap, cap + 1))
            self.full_res_min.append(lo)
            self.full_res_max.append(hi)
            if self.survivors[p]:
                lo, hi = self._residue_extremes(
                    e.counter, self.modulus, self.survivors[p])
            else:
                lo, hi = None, None
            self.res_min.append(lo)
            self.res_max.append(hi)

    # -- edge systems --------------------------------------------------------

    def _anchor_edges(self, edges):
        edges.append((0, self.anchor_i, self.anchor_wrap, {
            "kind": "anchor", "node": self.anchor_id, "side": "upper",
            "wrap": self.anchor_wrap}))
        edges.append((self.anchor_i, 0, -self.anchor_wrap, {
            "kind": "anchor", "node": self.anchor_id, "side": "lower",
            "wrap": self.anchor_wrap}))

    def _wrap_edges(self, c, r_source, r_target):
        m = self.modulus
        dc = r_target - r_source
        hi_k = (c.hi - dc) // m
        lo_k = _ceil_div(c.lo - dc, m)
        return (
            (self.idx[c.source], self.idx[c.target], hi_k, {
                "kind": "constraint_upper", "constraint": c.id,
                "source": c.source, "target": c.target, "bound": hi_k}),
            (self.idx[c.target], self.idx[c.source], -lo_k, {
                "kind": "constraint_lower", "constraint": c.id,
                "source": c.source, "target": c.target, "bound": lo_k}),
        )

    def _exact_edges(self, gi, value, residues, fixed):
        """Determined edges between candidate node ``gi`` and fixed nodes."""
        m = self.modulus
        rid = self.node_ids[gi - 1]
        r_i = (self.counters[rid] + value) % m
        out, into, selfw = [], [], []
        for gj, c, gi_is_source in self.incident[gi]:
            if gj == gi:
                # self-loop constraint: both ends are the node being fixed
                e_hi, e_lo = self._wrap_edges(c, r_i, r_i)
                selfw.append(e_hi[2])  # k_t - k_s <= hi_k  (weight hi_k)
                selfw.append(e_lo[2])  # k_t - k_s >= lo_k  (weight -lo_k)
                continue
            if not fixed[gj]:
                continue
            r_j = residues[gj]
            if gi_is_source:
                e_hi, e_lo = self._wrap_edges(c, r_i, r_j)
                # e_hi is gi -> gj, e_lo is gj -> gi.
                out.append((e_hi[1], e_hi[2], e_hi[3]))
                into.append((e_lo[0], e_lo[2], e_lo[3]))
            else:
                e_hi, e_lo = self._wrap_edges(c, r_j, r_i)
                # e_hi is gj -> gi, e_lo is gi -> gj.
                into.append((e_hi[0], e_hi[2], e_hi[3]))
                out.append((e_lo[1], e_lo[2], e_lo[3]))
        out.append((0, 0, {"kind": "non_negative", "node": rid}))
        return r_i, out, into, selfw

    def _optimistic_edges(self, assigned=(), pos=0,
                          singleton_p=None, singleton_v=None):
        """Loosest edge system attainable inside the correction domains.

        Variables before ``pos`` are pinned to ``assigned``; every other free
        variable ranges over the residues of its surviving domain.  With
        ``singleton_p``/``singleton_v`` given, that one variable is pinned
        while every other variable keeps its whole domain (root filtering).

        Every difference edge is set to its *largest* (loosest) weight over
        the domains, so the resulting system is a relaxation whose feasible
        region is a superset of every concrete completion's: a negative cycle
        here proves infeasibility for all completions.  (For a self-loop the
        independent residue extrema only loosen further, which stays sound.)
        All arithmetic is over normalized residues.
        """
        edges = []
        self._anchor_edges(edges)
        for e in self.events:
            edges.append((self.idx[e.id], 0, 0,
                          {"kind": "non_negative", "node": e.id}))

        def residue_extremes(graph_i):
            if graph_i == self.anchor_i:
                r = self.anchor["absolute"] % self.modulus
                return r, r
            # graph layout: ZERO=0, anchor=1, event p -> p + 2
            p = graph_i - 2
            counter = self.counters[self.events[p].id]
            if singleton_p is not None:
                if p == singleton_p:
                    r = (counter + singleton_v) % self.modulus
                    return r, r
                return self.full_res_min[p], self.full_res_max[p]
            if p < pos:
                r = (counter + assigned[p]) % self.modulus
                return r, r
            return self.res_min[p], self.res_max[p]

        for c in self.constraints:
            s, t = self.idx[c.source], self.idx[c.target]
            rs_min, rs_max = residue_extremes(s)
            rt_min, rt_max = residue_extremes(t)
            u_max = (c.hi - rt_min + rs_max) // self.modulus
            l_max = _ceil_div(c.lo - rt_max + rs_min, self.modulus)
            e_hi, e_lo = self._wrap_edges(c, 0, 0)
            edges.append((s, t, u_max, e_hi[3]))
            edges.append((t, s, -l_max, e_lo[3]))
        return edges

    def _leaf_edges(self, prefix):
        """Full exact edge system after committing ``prefix`` corrections."""
        m = self.modulus
        residues = {self.anchor_id: self.anchor["absolute"] % m}
        committed = {self.anchor_id}
        for p in range(len(prefix)):
            e = self.events[p]
            residues[e.id] = (e.counter + prefix[p]) % m
            committed.add(e.id)
        edges = []
        self._anchor_edges(edges)
        for eid in committed:
            if eid != self.anchor_id:
                edges.append((self.idx[eid], 0, 0,
                              {"kind": "non_negative", "node": eid}))
        for c in self.constraints:
            if c.source in committed and c.target in committed:
                e_hi, e_lo = self._wrap_edges(
                    c, residues[c.source], residues[c.target])
                edges.append(e_hi)
                edges.append(e_lo)
        return edges

    # -- incremental exact closure ------------------------------------------

    @staticmethod
    def _fresh_closure(n, anchor_i, anchor_wrap):
        dist = [[None] * n for _ in range(n)]
        for i in range(n):
            dist[i][i] = 0
        dist[0][anchor_i] = anchor_wrap
        dist[anchor_i][0] = -anchor_wrap
        return dist

    def _cycle_through(self, dist, gi, value, residues, fixed):
        """Worst negative cycle closed by fixing node ``gi`` to ``value``.

        Such a cycle consists of an out-edge gi->a, a shortest path a->b over
        already fixed nodes, and an in-edge b->gi.  A fixed self-loop with a
        negative weight is itself a cycle.  Returns None when the candidate
        keeps the system feasible.
        """
        _r, out, into, selfw = self._exact_edges(
            gi, value, residues, fixed)
        self_neg = min(selfw) if selfw else None
        worst = (self_neg, None, None) if self_neg is not None and self_neg < 0 else None
        for a, wo, _mo in out:
            for b, wi, _mi in into:
                base = dist[a][b]
                if base is None:
                    continue
                total = wo + base + wi
                if total < 0 and (worst is None or total < worst[0]):
                    worst = (total, a, b)
        return worst

    def _commit(self, dist, gi, value, residues, fixed):
        """Return a new all-pairs closure with node ``gi`` fixed, or prune."""
        witness = self._cycle_through(dist, gi, value, residues, fixed)
        if witness is not None:
            return None, witness
        _r, out, into, _selfw = self._exact_edges(
            gi, value, residues, fixed)
        old = [i for i in range(self.n) if fixed[i]]

        d_out, d_in = {}, {}
        for x in old:
            best = None
            for a, w, _m in out:
                if dist[a][x] is None:
                    continue
                cand = w + dist[a][x]
                if best is None or cand < best:
                    best = cand
            d_out[x] = best
        for x in old:
            best = None
            for b, w, _m in into:
                if dist[x][b] is None:
                    continue
                cand = dist[x][b] + w
                if best is None or cand < best:
                    best = cand
            d_in[x] = best

        nd = [row[:] for row in dist]
        for x in old:
            nd[gi][x] = d_out[x]
            nd[x][gi] = d_in[x]
        for x in old:
            if d_in[x] is None:
                continue
            for y in old:
                if d_out[y] is None:
                    continue
                cand = d_in[x] + d_out[y]
                if nd[x][y] is None or cand < nd[x][y]:
                    nd[x][y] = cand
        nd[gi][gi] = 0

        nres = residues[:]
        rid = self.node_ids[gi - 1]
        nres[gi] = (self.counters[rid] + value) % self.modulus
        nfixed = fixed[:]
        nfixed[gi] = True
        return (nd, nfixed, nres), None

    # -- evidence ------------------------------------------------------------

    def _bump(self, reason):
        self.stats["pruned"][reason] = self.stats["pruned"].get(reason, 0) + 1

    def _record_sample(self, reason, depth, assigned, kind,
                       prefix=None, opt_edges=None):
        if len(self.samples) >= SAMPLE_LIMIT:
            return
        sample = {
            "reason": reason,
            "depth": depth,
            "assigned": [
                {"event": self.events[p].id, "correction": assigned[p]}
                for p in range(depth)
            ],
        }
        if kind == "exact":
            chain = _conflict_chain(self.n, self._leaf_edges(prefix),
                                    self.labels)
        else:
            chain = self._optimistic_chain(opt_edges)
        if chain is not None:
            sample["conflict_chain"] = chain
        self.samples.append(sample)

    def _optimistic_chain(self, edges):
        cycle = _negative_cycle_edges(self.n, edges)
        if cycle is None:
            return None
        steps, nodes = [], []
        total = 0
        for pos, ei in enumerate(cycle):
            u, v, w, meta = edges[ei]
            total += w
            if pos == 0:
                nodes.append(self.labels[u])
            nodes.append(self.labels[v])
            step = {
                "edge": f"{self.labels[u]}->{self.labels[v]}",
                "kind": "domain_bound",
                "weight": w,
                "relation": (
                    "tightest wrap bound attainable inside the free "
                    "correction domains"),
            }
            if meta and "constraint" in meta:
                step["constraint"] = meta["constraint"]
            steps.append(step)
        return {
            "constraints": sorted({s["constraint"] for s in steps
                                   if "constraint" in s}),
            "cycle": nodes,
            "steps": steps,
            "total_weight": total,
            "explanation": (
                "even granting every unassigned correction its whole domain, "
                f"the tightest bounds form a negative cycle summing to "
                f"{total} < 0"),
        }

    # -- branch and bound ----------------------------------------------------

    def _forced_nonzero(self, dist, fixed, residues, pos):
        """Variables a conflict chain forces away from zero at this node."""
        forced = []
        for p in range(pos, self.m):
            if self._cycle_through(dist, p + 2, 0, residues, fixed) is not None:
                forced.append(p)
        return forced

    def run(self):
        self.filter_domains()
        if self.root_empty is not None:
            p = self.root_empty
            v = 0 if 0 in self.removed[p] else next(iter(self.removed[p]))
            self._record_sample(
                "root_domain_empty", 0, [], "optimistic",
                opt_edges=self._optimistic_edges(
                    singleton_p=p, singleton_v=v))
            return {"outcome": "exhausted", "complete": True, "cut_off": False}

        dist0 = self._fresh_closure(self.n, self.anchor_i, self.anchor_wrap)
        fixed0 = [False] * self.n
        fixed0[0] = fixed0[self.anchor_i] = True
        residues0 = [None] * self.n
        residues0[self.anchor_i] = self.anchor["absolute"] % self.modulus

        cut_off = False
        try:
            self._dfs(0, dist0, fixed0, residues0, [], 0, 0)
        except _BudgetReached:
            cut_off = True

        # A cut-off traversal cannot certify global optimality even when a
        # feasible incumbent was found, so it is reported as incomplete
        # exhaustion rather than a canonical repair.
        if self.best is not None and not cut_off:
            return {"outcome": "repaired", "vector": self.best[2]}
        return {"outcome": "exhausted", "complete": not cut_off,
                "cut_off": cut_off}

    def _dfs(self, pos, dist, fixed, residues, assigned, changed, abs_sum):
        self.stats["nodes"] += 1
        if self.stats["nodes"] > SEARCH_BUDGET:
            raise _BudgetReached()
        if pos > self.stats["deepest"]:
            self.stats["deepest"] = pos

        # Necessary-feasibility prune over the free correction domains.
        opt_edges = self._optimistic_edges(assigned, pos)
        opt_dist = _floyd_warshall(self.n, opt_edges)
        if any(opt_dist[i][i] < 0 for i in range(self.n)):
            self._bump("interval_closure")
            self._record_sample("interval_closure", pos, assigned,
                                "optimistic", opt_edges=opt_edges)
            return

        # Conflict-chain lower bounds on the remaining corrections.
        forced = self._forced_nonzero(dist, fixed, residues, pos)
        lb_changed = changed + len(forced)
        lb_abs = abs_sum + len(forced)
        if self.best is not None:
            best_changed, best_abs, _ = self.best
            if lb_changed > best_changed:
                self._bump("lower_bound_changed")
                return
            if lb_changed == best_changed and lb_abs > best_abs:
                self._bump("lower_bound_abs")
                return

        if pos == self.m:
            vector = tuple(assigned)
            cand = (changed, abs_sum, vector)
            if self.best is None or cand < self.best:
                self.best = cand
            return

        for v in self.candidates(pos):
            committed, witness = self._commit(
                dist, pos + 2, v, residues, fixed)
            if committed is None:
                self._bump("negative_cycle")
                self._record_sample(
                    "negative_cycle", pos + 1, assigned + [v], "exact",
                    prefix=assigned + [v])
                continue
            nd, nfixed, nres = committed
            self._dfs(pos + 1, nd, nfixed, nres, assigned + [v],
                      changed + (1 if v != 0 else 0), abs_sum + abs(v))

    # -- result assembly -----------------------------------------------------

    def repaired(self, vector):
        m = self.modulus
        corrections, adjusted_events, changed = [], [], []
        abs_sum = 0
        for e, delta in zip(self.events, vector):
            corrected = (e.counter + delta) % m
            corrections.append({
                "id": e.id,
                "original_counter": e.counter,
                "correction": delta,
                "corrected_counter": corrected,
            })
            adjusted_events.append(Event(e.id, corrected))
            if delta != 0:
                changed.append(e.id)
                abs_sum += abs(delta)

        adj_norm = {
            "request_id": self.req.norm["request_id"],
            "modulus": m,
            "anchor": self.anchor,
            "events": adjusted_events,
            "constraints": self.constraints,
        }
        result = solve(adj_norm)
        if result["status"] == "unique":
            canonical = result["conclusion"]["timeline"]
        else:
            canonical = result["conclusion"]["timelines"][0]
        at = {row["id"]: row for row in canonical}

        constraint_deltas = []
        for c in self.constraints:
            s, t = at[c.source], at[c.target]
            dc = t["counter"] - s["counter"]
            lo_k = _ceil_div(c.lo - dc, m)
            hi_k = (c.hi - dc) // m
            abs_delta = t["absolute"] - s["absolute"]
            constraint_deltas.append({
                "constraint": c.id,
                "source": c.source,
                "target": c.target,
                "interval": [c.lo, c.hi],
                "corrected_counter_delta": dc,
                "wrap_bounds": [lo_k, hi_k],
                "wrap_delta": t["wrap"] - s["wrap"],
                "absolute_delta": abs_delta,
                "satisfied": c.lo <= abs_delta <= c.hi,
            })

        conclusion = {
            "canonical_corrections": corrections,
            "changed_events": changed,
            "changed_count": len(changed),
            "abs_correction_sum": abs_sum,
            "timeline": canonical,
            "constraint_deltas": constraint_deltas,
            "summary": (
                f"{len(changed)} event counter(s) corrected with total tick "
                f"effort {abs_sum}; causal windows unchanged"),
        }
        evidence = {
            "k": self.k_cap,
            "domains": self._domain_report(),
            "search": self._search_report(complete=True),
            "repaired_status": result["status"],
        }
        return conclusion, evidence

    def exhausted(self, outcome):
        body = {
            "k": self.k_cap,
            "complete_proof": outcome["complete"],
            "search_budget": SEARCH_BUDGET,
            "nodes_explored": self.stats["nodes"],
            "deepest_depth": self.stats["deepest"],
            "branches_pruned": dict(sorted(self.stats["pruned"].items())),
            "domains": self._domain_report(),
            "sample_branches": self.samples,
            "recomputation": (
                "re-run the same frozen audit with the same k: the "
                "candidate order 0, +1, -1, +2, -2, ... and the reported "
                "prune reasons reproduce this traversal; every sampled "
                "conflict chain sums the shown integer weights < 0"),
        }
        if outcome.get("cut_off") and self.best is not None:
            # Only a feasibility incumbent, never certified optimal: surfaced
            # for recomputation, not as a canonical repair.
            changed, abs_sum, vector = self.best
            body["uncertified_incumbent"] = {
                "corrections": list(vector),
                "changed_count": changed,
                "abs_correction_sum": abs_sum,
                "note": ("feasible vector found before the budget was hit, "
                         "but remaining branches were not explored, so it is "
                         "not a certified optimum"),
            }
        if outcome["complete"]:
            summary = (
                "no correction within [-k, k] reconciles the frozen "
                "constraints; the source audit is left unchanged")
        else:
            summary = (
                "the search budget was exhausted before feasibility could be "
                "settled; no correction is certified and the source audit is "
                "left unchanged")
        conclusion = {"exhausted": body, "summary": summary}
        evidence = {
            "search": self._search_report(complete=outcome["complete"]),
        }
        return conclusion, evidence

    def _domain_report(self):
        return [
            {
                "event": e.id,
                "min": -self.k_cap,
                "max": self.k_cap,
                "eliminated": sorted(self.removed[p]),
                "surviving_corrections": self.survivors[p],
                "residue_extremes": [self.res_min[p], self.res_max[p]],
            }
            for p, e in enumerate(self.events)
        ]

    def _search_report(self, complete):
        return {
            "nodes_explored": self.stats["nodes"],
            "search_budget": SEARCH_BUDGET,
            "deepest_depth": self.stats["deepest"],
            "branches_pruned": dict(sorted(self.stats["pruned"].items())),
            "complete": complete,
        }


def solve_repair(req: RepairRequest):
    """Run the joint correction/wrap search for a validated request."""
    search = _Search(req)
    outcome = search.run()
    if outcome["outcome"] == "repaired":
        conclusion, evidence = search.repaired(outcome["vector"])
        return "repaired", conclusion, evidence
    conclusion, evidence = search.exhausted(outcome)
    return "exhausted", conclusion, evidence
