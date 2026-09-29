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

from .solver import normalize, solve


class ConflictError(Exception):
    """request_id was already used with a different payload."""

    def __init__(self, audit_id):
        self.audit_id = audit_id
        super().__init__(f"request_id already bound to {audit_id}")


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
