"""Run the dedicated broker-inert Mala consultation HTTP process."""

from __future__ import annotations

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from typing import Any

from bhiksha.trader_desk.consult_service import (
    BrokerInertConsultationService,
    ConsultServiceConfig,
)


MAX_REQUEST_BYTES = 16_384


class ConsultationHandler(BaseHTTPRequestHandler):
    service: BrokerInertConsultationService

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/api/status":
            self._send_json(self.service.status())
        elif self.path == "/api/preflight":
            self._send_json(self.service.preflight())
        elif self.path == "/api/latest":
            self._send_json({"latest": self.service.latest()})
        else:
            self._send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/consult":
            self._send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_REQUEST_BYTES:
                raise ValueError("invalid request size")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("JSON body must be an object")
            result = self.service.consult(payload)
        except (ValueError, json.JSONDecodeError):
            self._send_error(HTTPStatus.BAD_REQUEST)
            return
        self._send_json(result)

    def do_HEAD(self) -> None:  # noqa: N802
        self._send_error(HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args: Any) -> None:
        return None

    def _send_json(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: HTTPStatus) -> None:
        body = b'{"detail":"request rejected"}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--mala-repo", type=Path, required=True)
    parser.add_argument("--capability-manifest", type=Path, required=True)
    parser.add_argument("--legacy-retirement-report", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, default=Path("artifacts/playbook"))
    args = parser.parse_args(argv)
    if args.host not in {"127.0.0.1", "localhost"}:
        parser.error("consultation service requires a loopback host")
    service = BrokerInertConsultationService(
        ConsultServiceConfig(
            packet=args.packet,
            mala_repo=args.mala_repo,
            capability_manifest=args.capability_manifest,
            legacy_retirement_report=args.legacy_retirement_report,
            artifact_root=args.artifact_root,
        )
    )
    ConsultationHandler.service = service
    server = ThreadingHTTPServer((args.host, args.port), ConsultationHandler)
    print(
        f"Bhiksha broker-inert consultation serving at "
        f"http://{args.host}:{server.server_port}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
