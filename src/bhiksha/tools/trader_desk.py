"""Serve the Bhiksha Trader Desk sidecar UI."""

from __future__ import annotations

import argparse
from functools import partial
import json
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from bhiksha.trader_desk.service import (
    DEFAULT_ARTIFACT_ROOT,
    DEFAULT_CAPABILITY_MANIFEST,
    DEFAULT_LEGACY_REPORT,
    DEFAULT_MALA_REPO,
    DEFAULT_PACKET,
    TraderDeskConfig,
    TraderDeskService,
)


STATIC_ROOT = Path(__file__).resolve().parents[1] / "trader_desk" / "static"


class TraderDeskHandler(SimpleHTTPRequestHandler):
    service: TraderDeskService

    def __init__(self, *args: Any, service: TraderDeskService, **kwargs: Any) -> None:
        self.service = service
        super().__init__(*args, directory=str(STATIC_ROOT), **kwargs)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/status":
            include_health = parse_qs(parsed.query).get("health", ["0"])[0] in {"1", "true", "yes"}
            self._send_json(self.service.status(include_health=include_health))
            return
        if parsed.path == "/api/health":
            self._send_json(self.service.health())
            return
        if parsed.path == "/api/preflight":
            self._send_json(self.service.preflight())
            return
        if parsed.path == "/api/latest":
            latest = self.service.latest_artifacts()
            self._send_json({"latest": latest})
            return
        if parsed.path == "/api/live-context":
            payload = {key: value[0] for key, value in parse_qs(parsed.query).items() if value}
            self._send_json(self.service.live_context(payload))
            return
        if parsed.path == "/api/live-management/status":
            self._send_json(self.service.live_management_status())
            return
        if parsed.path == "/":
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        routes = {
            "/api/consult": self.service.consult,
            "/api/decision": self.service.decide,
            "/api/option-preview": self.service.preview_option,
            "/api/live-ticket": self.service.live_ticket,
            "/api/approve-submit": self.service.approve_submit,
        }
        parsed = urlparse(self.path)
        handler = routes.get(parsed.path)
        if handler is None:
            self._send_error(HTTPStatus.NOT_FOUND, "unknown endpoint")
            return
        try:
            payload = self._read_json()
            self._send_json(handler(payload))
        except Exception as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, str(exc))

    def log_message(self, format: str, *args: Any) -> None:
        return None

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("content-length", "0") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: HTTPStatus, message: str) -> None:
        self._send_json({"error": message, "status": "error"}, status=status)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--packet", type=Path, default=DEFAULT_PACKET)
    parser.add_argument("--mala-repo", type=Path, default=DEFAULT_MALA_REPO)
    parser.add_argument("--capability-manifest", type=Path, default=DEFAULT_CAPABILITY_MANIFEST)
    parser.add_argument("--legacy-retirement-report", type=Path, default=DEFAULT_LEGACY_REPORT)
    parser.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--db-path", type=Path, default=Path("bhiksha.db"))
    parser.add_argument("--update-mala-log", action="store_true")
    args = parser.parse_args(argv)

    service = TraderDeskService(
        TraderDeskConfig(
            packet=args.packet,
            mala_repo=args.mala_repo,
            capability_manifest=args.capability_manifest,
            legacy_retirement_report=args.legacy_retirement_report,
            artifact_root=args.artifact_root,
            db_path=args.db_path,
            update_mala_log=args.update_mala_log,
        )
    )
    handler = partial(TraderDeskHandler, service=service)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    url = f"http://{args.host}:{server.server_port}"
    print(f"Bhiksha Trader Desk serving at {url}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
