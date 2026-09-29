"""Idempotent, in-memory audit store.

Creation is keyed by ``request_id``: replaying the same identifier with the
same payload returns the original audit record, while the same identifier
with any event or constraint changed is rejected and adds no record.  Every
stored record freezes the input, conclusion and evidence for later reads.

Counter repairs live in a second, append-only table keyed by ``fix_id``:
the source audit and the correction budget K are frozen at creation,
retransmitting the same fix_id replays the original repair record, and
reusing a fix_id against another source (or with another K) is rejected
without ever rewriting the source audit.
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
from datetime import datetime, timezone

from .repair import plan_repair
from .solver import normalize, solve


class ConflictError(Exception):
    """request_id was already used with a different payload."""

    def __init__(self, audit_id):
        self.audit_id = audit_id
        super().__init__(f"request_id already bound to {audit_id}")


class RepairConflictError(Exception):
    """fix_id is already bound to another source audit or another K."""

    def __init__(self, repair_id, source_audit_id, K):
        self.repair_id = repair_id
        self.source_audit_id = source_audit_id
        self.K = K
        super().__init__(
            f"fix_id already bound to {repair_id} on {source_audit_id} "
            f"with K={K}")


class SourceNotRepairable(Exception):
    """Only an unsatisfiable audit may receive counter repairs."""

    def __init__(self, status):
        self.status = status
        super().__init__(f"source audit status is {status}, not unsatisfiable")


def canonical_form(payload):
    """Order-insensitive rendering of the semantic payload for fingerprinting."""
    body = {
        "modulus": payload.get("modulus"),
        "anchor": payload.get("anchor"),
        "events": sorted(payload.get("events") or [], key=lambda e: e.get("id", "")),
        "constraints": sorted(
            payload.get("constraints") or [], key=lambda c: c.get("id", "")),
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


class AuditStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._by_request = {}  # request_id -> (fingerprint, audit_id)
        self._audits = {}      # audit_id -> frozen record
        self._order = []       # audit ids in creation order
        self._seq = 0
        self._by_fix = {}      # fix_id -> repair record
        self._repairs = {}     # repair_id -> frozen record
        self._repair_order = []  # repair ids in creation order
        self._repair_seq = 0

    def create(self, payload):
        """Return (record, created). Raises InputError or ConflictError."""
        norm = normalize(payload)  # invalid payloads never claim a request_id
        fingerprint = hashlib.sha256(
            canonical_form(payload).encode("utf-8")).hexdigest()
        with self._lock:
            prior = self._by_request.get(norm["request_id"])
            if prior is not None:
                if prior[0] == fingerprint:
                    return self._audits[prior[1]], False
                raise ConflictError(prior[1])
            result = solve(norm)
            self._seq += 1
            audit_id = f"AUD-{self._seq:06d}"
            record = {
                "audit_id": audit_id,
                "request_id": norm["request_id"],
                "created_at": datetime.now(timezone.utc).isoformat(),
                "input": copy.deepcopy(payload),
                "status": result["status"],
                "conclusion": result["conclusion"],
                "evidence": result["evidence"],
            }
            self._by_request[norm["request_id"]] = (fingerprint, audit_id)
            self._audits[audit_id] = record
            self._order.append(audit_id)
            return record, True

    def get(self, audit_id):
        return self._audits.get(audit_id)

    def ids(self):
        return list(self._order)

    def count(self):
        return len(self._order)

    # ------------------------------------------------------------------
    # counter repairs
    # ------------------------------------------------------------------

    def create_repair(self, audit_id, req):
        """Freeze ``(source audit, K)`` and create/return a repair record.

        ``req`` must already be validated via ``validate_repair_request``.
        Returns ``(record, created)``.  The source audit is never mutated.
        Raises RepairConflictError or SourceNotRepairable.
        """
        fix_id = req["fix_id"]
        K = req["K"]
        with self._lock:
            source = self._audits.get(audit_id)
            if source is None:
                raise KeyError(audit_id)
            prior = self._by_fix.get(fix_id)
            if prior is not None:
                if (prior["source_audit_id"] == audit_id
                        and prior["K"] == K):
                    return prior, False
                raise RepairConflictError(
                    prior["repair_id"], prior["source_audit_id"], prior["K"])
            if source["status"] != "unsatisfiable":
                raise SourceNotRepairable(source["status"])
            norm = normalize(source["input"])
            plan = plan_repair(norm, K)
            self._repair_seq += 1
            repair_id = f"FIX-{self._repair_seq:06d}"
            record = {
                "repair_id": repair_id,
                "fix_id": fix_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "source_audit_id": audit_id,
                "source_request_id": source["request_id"],
                "K": K,
                "frozen_source": copy.deepcopy(source["input"]),
                "frozen_modulus": source["input"]["modulus"],
                "original_conflict_chain": copy.deepcopy(
                    source["conclusion"]["conflict_chain"]),
                "status": plan["status"],
                **{k: copy.deepcopy(v) for k, v in plan.items()
                   if k != "status"},
            }
            self._by_fix[fix_id] = record
            self._repairs[repair_id] = record
            self._repair_order.append(repair_id)
            return record, True

    def get_repair(self, repair_id):
        return self._repairs.get(repair_id)

    def repair_by_fix_id(self, fix_id):
        return self._by_fix.get(fix_id)

    def repair_ids(self):
        return list(self._repair_order)

    def repair_ids_for(self, audit_id):
        return [rid for rid in self._repair_order
                if self._repairs[rid]["source_audit_id"] == audit_id]

    def repair_count(self):
        return len(self._repair_order)
