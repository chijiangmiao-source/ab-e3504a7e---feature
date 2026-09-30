"""HTTP API for the deep-space timestamp audit service (stdlib only).

Endpoints:
    GET  /health                 liveness probe
    POST /audits                 create an audit (idempotent on request_id)
    GET  /audits                 list audit ids
    GET  /audits/{id}            read the frozen input, conclusion and evidence
    POST /audits/{id}/repairs    repair an unsatisfiable audit's counters
    GET  /repairs/{number}       read a frozen repair record
"""

from __future__ import annotations

import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .solver import InputError
from .store import (
    AuditNotRepairableError,
    AuditStore,
    ConflictError,
    RepairConflictError,
    UnknownAuditError,
)

_AUDIT_PATH = re.compile(r"^/audits/([A-Za-z0-9][A-Za-z0-9_-]*)$")
_REPAIRS_PATH = re.compile(
    r"^/audits/([A-Za-z0-9][A-Za-z0-9_-]*)/repairs$")
_REPAIR_PATH = re.compile(r"^/repairs/([A-Za-z0-9][A-Za-z0-9_-]*)$")


class _BadBody(Exception):
    pass


class Handler(BaseHTTPRequestHandler):
    server_version = "DeepSpaceAudit/1.0"
    store = AuditStore()

    # -- helpers -----------------------------------------------------------

    def _send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise _BadBody()
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            return json.loads(raw.decode("utf-8") or "null")
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise _BadBody()

    def log_message(self, fmt, *args):  # keep container logs free of probe spam
        pass

    # -- routes ------------------------------------------------------------

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/health":
            return self._send_json(200, {"status": "ok", "service": "deep-space-audit"})
        if path == "/audits":
            return self._send_json(200, {
                "count": self.store.count(),
                "audits": self.store.ids(),
            })
        match = _REPAIR_PATH.match(path)
        if match:
            record = self.store.get_repair(match.group(1))
            if record is None:
                return self._send_json(404, {
                    "error": "not_found",
                    "message": f"no repair {match.group(1)}",
                })
            return self._send_json(200, record)
        match = _AUDIT_PATH.match(path)
        if match:
            record = self.store.get(match.group(1))
            if record is None:
                return self._send_json(404, {
                    "error": "not_found",
                    "message": f"no audit {match.group(1)}",
                })
            body = dict(record)
            body["repairs"] = self.store.repair_numbers(record["audit_id"])
            return self._send_json(200, body)
        return self._send_json(404, {"error": "not_found"})

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        repair_match = _REPAIRS_PATH.match(path)
        if repair_match is None and path != "/audits":
            return self._send_json(404, {"error": "not_found"})
        try:
            payload = self._read_json()
        except _BadBody:
            return self._send_json(400, {
                "error": "bad_json",
                "message": "request body is not valid JSON",
            })
        if repair_match is not None:
            return self._create_repair(repair_match.group(1), payload)
        try:
            record, created = self.store.create(payload)
        except InputError as exc:
            return self._send_json(400, {
                "error": "invalid_input",
                "problems": exc.problems,
            })
        except ConflictError as exc:
            return self._send_json(409, {
                "error": "request_id_conflict",
                "message": "request_id was already used with a different payload; "
                           "no record was added",
                "existing_audit_id": exc.audit_id,
            })
        body = dict(record)
        body["replayed"] = not created
        return self._send_json(201 if created else 200, body)

    def _create_repair(self, audit_id, payload):
        try:
            record, created = self.store.create_repair(audit_id, payload)
        except UnknownAuditError as exc:
            return self._send_json(404, {
                "error": "not_found",
                "message": f"no audit {exc.audit_id}",
            })
        except AuditNotRepairableError as exc:
            return self._send_json(409, {
                "error": "audit_not_unsatisfiable",
                "message": (f"audit {exc.audit_id} is concluded "
                            f"'{exc.status}'; only unsatisfiable audits accept "
                            "counter repairs"),
                "audit_id": exc.audit_id,
                "audit_status": exc.status,
            })
        except InputError as exc:
            return self._send_json(400, {
                "error": "invalid_repair",
                "problems": exc.problems,
            })
        except RepairConflictError as exc:
            return self._send_json(409, {
                "error": "repair_id_conflict",
                "message": (
                    "repair_id was already used with a different source audit "
                    "or a different k; the source audit is unchanged"),
                "existing_repair_number": exc.repair_number,
                "existing_audit_id": exc.audit_id,
            })
        body = dict(record)
        body["replayed"] = not created
        return self._send_json(201 if created else 200, body)


def make_server(host, port, store=None):
    handler = Handler
    if store is not None:
        class _BoundHandler(Handler):
            pass
        _BoundHandler.store = store
        handler = _BoundHandler
    return ThreadingHTTPServer((host, port), handler)


def main():
    port = int(os.environ.get("PORT", "8080"))
    server = make_server("0.0.0.0", port)
    print(f"deep-space-audit listening on 0.0.0.0:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
