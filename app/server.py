"""HTTP API for the deep-space timestamp audit service (stdlib only).

Endpoints:
    GET    /health                     liveness probe
    POST   /audits                     create an audit (idempotent on request_id)
    GET    /audits                     list audit ids
    GET    /audits/{id}                read the frozen input, conclusion and evidence
    POST   /audits/{id}/repairs        submit a counter repair for an unsat audit
    GET    /audits/{id}/repairs        list repairs recorded against an audit
    GET    /repairs/{rid}              read a frozen repair record
"""

from __future__ import annotations

import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .repair import validate_repair_request
from .solver import InputError
from .store import (
    AuditStore,
    ConflictError,
    RepairConflictError,
    SourceNotRepairable,
)

_AUDIT_PATH = re.compile(r"^/audits/([A-Za-z0-9][A-Za-z0-9_-]*)$")
_AUDIT_REPAIRS_PATH = re.compile(
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
        repairs_match = _AUDIT_REPAIRS_PATH.match(path)
        if repairs_match:
            record = self.store.get(repairs_match.group(1))
            if record is None:
                return self._send_json(404, {
                    "error": "not_found",
                    "message": f"no audit {repairs_match.group(1)}",
                })
            return self._send_json(200, {
                "audit_id": record["audit_id"],
                "count": len(self.store.repair_ids_for(record["audit_id"])),
                "repairs": self.store.repair_ids_for(record["audit_id"]),
            })
        repair_match = _REPAIR_PATH.match(path)
        if repair_match:
            record = self.store.get_repair(repair_match.group(1))
            if record is None:
                return self._send_json(404, {
                    "error": "not_found",
                    "message": f"no repair {repair_match.group(1)}",
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
            return self._send_json(200, record)
        return self._send_json(404, {"error": "not_found"})

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        repairs_match = _AUDIT_REPAIRS_PATH.match(path)
        if repairs_match:
            return self._post_repair(repairs_match.group(1))
        if path != "/audits":
            return self._send_json(404, {"error": "not_found"})
        try:
            payload = self._read_json()
        except _BadBody:
            return self._send_json(400, {
                "error": "bad_json",
                "message": "request body is not valid JSON",
            })
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

    def _post_repair(self, audit_id):
        source = self.store.get(audit_id)
        if source is None:
            return self._send_json(404, {
                "error": "not_found",
                "message": f"no audit {audit_id}",
            })
        try:
            body = self._read_json()
        except _BadBody:
            return self._send_json(400, {
                "error": "bad_json",
                "message": "request body is not valid JSON",
            })
        try:
            req = validate_repair_request(body, source["input"]["modulus"])
        except InputError as exc:
            return self._send_json(400, {
                "error": "invalid_input",
                "problems": exc.problems,
            })
        try:
            record, created = self.store.create_repair(audit_id, req)
        except RepairConflictError as exc:
            return self._send_json(409, {
                "error": "fix_id_conflict",
                "message": (
                    "fix_id was already used on "
                    f"{exc.source_audit_id} with K={exc.K}; a different "
                    "source audit or K is rejected and no record changed"),
                "existing_repair_id": exc.repair_id,
                "bound_source_audit_id": exc.source_audit_id,
                "bound_K": exc.K,
            })
        except SourceNotRepairable as exc:
            return self._send_json(409, {
                "error": "source_not_unsatisfiable",
                "message": (
                    "counter repairs are only accepted against an "
                    f"unsatisfiable audit; source status is {exc.status}"),
                "source_status": exc.status,
            })
        out = dict(record)
        out["replayed"] = not created
        return self._send_json(201 if created else 200, out)


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
