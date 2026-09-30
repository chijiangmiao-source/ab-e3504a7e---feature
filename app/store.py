"""Idempotent, in-memory audit store.

Creation is keyed by ``request_id``: replaying the same identifier with the
same payload returns the original audit record, while the same identifier
with any event or constraint changed is rejected and adds no record.  Every
stored record freezes the input, conclusion and evidence for later reads.
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
from datetime import datetime, timezone

from .repair import normalize_repair, solve_repair
from .solver import normalize, solve


class ConflictError(Exception):
    """request_id was already used with a different payload."""

    def __init__(self, audit_id):
        self.audit_id = audit_id
        super().__init__(f"request_id already bound to {audit_id}")


class RepairConflictError(Exception):
    """repair_id was already used with a different source audit or k."""

    def __init__(self, repair_number, audit_id):
        self.repair_number = repair_number
        self.audit_id = audit_id
        super().__init__(
            f"repair_id already bound to {repair_number} on {audit_id}")


class AuditNotRepairableError(Exception):
    """Only audits concluded unsatisfiable can be repaired."""

    def __init__(self, audit_id, status):
        self.audit_id = audit_id
        self.status = status
        super().__init__(f"audit {audit_id} is {status}, not unsatisfiable")


class UnknownAuditError(Exception):
    def __init__(self, audit_id):
        self.audit_id = audit_id
        super().__init__(f"no audit {audit_id}")


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
        self._by_repair = {}   # repair_id -> (fingerprint, repair_number)
        self._repairs = {}     # repair_number -> frozen repair record
        self._audit_repairs = {}  # audit_id -> [repair_number]
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
            self._audit_repairs[audit_id] = []
            return record, True

    def create_repair(self, audit_id, body):
        """Create (or replay) a counter-correction repair on a frozen audit.

        Returns ``(record, created)``.  The source audit is reopened
        read-only: its frozen input, conclusion and evidence are never
        rewritten.  A repair_id replayed with the same source audit and k
        returns the original repair record; a different source audit or k is
        rejected with RepairConflictError and adds nothing.
        """
        with self._lock:
            source = self._audits.get(audit_id)
            if source is None:
                raise UnknownAuditError(audit_id)
            if source["status"] != "unsatisfiable":
                raise AuditNotRepairableError(audit_id, source["status"])

            # Re-normalize the frozen input: it was validated at creation, so
            # this only rebuilds the solver-ready structure.
            norm = normalize(source["input"])
            req = normalize_repair(body, norm)
            fingerprint = hashlib.sha256(
                json.dumps(
                    {"audit_id": audit_id, "k": req.k},
                    sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()

            prior = self._by_repair.get(req.repair_id)
            if prior is not None:
                if prior[0] == fingerprint:
                    return self._repairs[prior[1]], False
                raise RepairConflictError(prior[1], self._repair_audit(prior[1]))

            status, conclusion, evidence = solve_repair(req)
            self._repair_seq += 1
            number = f"FIX-{self._repair_seq:06d}"
            record = {
                "repair_number": number,
                "repair_id": req.repair_id,
                "audit_id": audit_id,
                "request_id": source["request_id"],
                "created_at": datetime.now(timezone.utc).isoformat(),
                "k": req.k,
                "status": status,
                "conclusion": conclusion,
                "evidence": evidence,
            }
            self._by_repair[req.repair_id] = (fingerprint, number)
            self._repairs[number] = record
            self._audit_repairs[audit_id].append(number)
            return record, True

    def _repair_audit(self, number):
        return self._repairs[number]["audit_id"]

    def get(self, audit_id):
        return self._audits.get(audit_id)

    def get_repair(self, number):
        return self._repairs.get(number)

    def repair_numbers(self, audit_id):
        return list(self._audit_repairs.get(audit_id, ()))

    def ids(self):
        return list(self._order)

    def count(self):
        return len(self._order)
