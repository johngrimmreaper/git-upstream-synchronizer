"""Loopback web adapter with SDK-free direct execution by default."""

import argparse
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
import logging
from pathlib import Path
import secrets

from .comparison import compare_repository
from .config import load_repositories
from .sync import GitCommandError, SyncError, synchronize_repository

LOG = logging.getLogger(__name__)

ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}


class WebServer(ThreadingHTTPServer):
    def __init__(self, repositories: dict, port: int = 8089, executor=None):
        self.repositories = repositories
        self.executor = executor
        self.token = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", port), RequestHandler)
        self.allowed_hosts = {f"127.0.0.1:{self.server_port}", f"localhost:{self.server_port}"}


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "GitUpstreamSynchronizer"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def _respond(self, status: int, body: bytes, content_type: str):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data: dict):
        self._respond(status, json.dumps(data).encode(), "application/json; charset=utf-8")

    def _trusted(self) -> bool:
        host = self.headers.get("Host", "")
        if len(self.headers.get_all("Host", [])) != 1 or len(self.headers.get_all("Origin", [])) > 1:
            self._json(403, {"error": "Invalid Host/Origin headers"})
            return False
        origin = self.headers.get("Origin")
        if host not in self.server.allowed_hosts or (
            origin is not None and origin != f"http://{host}"
        ):
            self._json(403, {"error": "Use the local server address"})
            return False
        return True

    def do_GET(self):
        if not self._trusted():
            return
        if self.path == "/api/repositories":
            self._json(200, {"execution": "camunda" if self.server.executor else "direct", "repositories": [
                {"id": name, "branch": config.get("branch", "main"),
                 "upstream": config.get("upstream", "upstream")}
                for name, config in self.server.repositories.items()
            ]})
        elif self.path in ASSETS:
            name, content_type = ASSETS[self.path]
            body = files("git_upstream_synchronizer").joinpath("web_assets", name).read_text(
                encoding="utf-8",
            )
            if name == "index.html":
                body = body.replace("__REQUEST_TOKEN__", self.server.token)
            self._respond(200, body.encode(), content_type)
        else:
            self._json(404, {"error": "Not found"})

    def do_POST(self):
        self.close_connection = True  # Do not reuse a connection with an unread/rejected body.
        if not self._trusted():
            return
        token = self.headers.get("X-Sync-Token", "")
        if not token.isascii() or not secrets.compare_digest(token, self.server.token):
            self._json(403, {"error": "Reload the page before trying again"})
            return
        if self.path not in {"/api/compare", "/api/sync", "/api/operation"}:
            self._json(404, {"error": "Not found"})
            return
        try:
            if self.headers.get_content_type() != "application/json":
                raise ValueError("Expected application/json")
            if self.headers.get("Transfer-Encoding"):
                raise ValueError("Transfer encoding is unsupported")
            if len(self.headers.get_all("Content-Length", [])) != 1:
                raise ValueError("Provide one Content-Length header")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError("Request body must contain 1 to 4096 bytes")
            data = json.loads(self.rfile.read(length))
            expected = {"repository_id", "operation_id"} if self.path == "/api/operation" else {"repository_id"}
            if not isinstance(data, dict) or set(data) != expected:
                raise ValueError("Invalid request fields")
            repository_id = data["repository_id"]
            if not isinstance(repository_id, str) or repository_id not in self.server.repositories:
                raise ValueError("Unknown repository_id")
            if self.path == "/api/operation":
                if not self.server.executor:
                    self._json(404, {"error": "No Camunda operations in direct mode"})
                    return
                operation_id = data["operation_id"]
                if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 128:
                    raise ValueError("Invalid operation_id")
                from .camunda_web import OperationNotFound
                try:
                    result = self.server.executor.status(operation_id, repository_id)
                except OperationNotFound:
                    self._json(404, {"error": "Unknown operation for this repository"})
                    return
                self._json(200, result)
                return
            if self.path == "/api/sync" and self.server.executor:
                from .camunda_web import OperationCapacityError
                try:
                    result = self.server.executor.submit(repository_id)
                except OperationCapacityError as error:
                    self._json(503, {"error": str(error)})
                    return
                self._json(202, result)
                return
            config = self.server.repositories[repository_id]
            options = {"branch": config.get("branch", "main"),
                       "upstream": config.get("upstream", "upstream")}
            if self.path == "/api/compare":
                result = compare_repository(Path(config["path"]), **options)
            else:
                result = asdict(synchronize_repository(Path(config["path"]), **options))
            self._json(200, result)
        except (ValueError, UnicodeError) as error:
            self._json(400, {"error": str(error)})
        except SyncError as error:
            self._json(409, {"error": str(error)})
        except GitCommandError as error:
            LOG.exception("Git request failed: route=%s", self.path)
            self._json(503, {"error": "Git operation failed; inspect the server log"})
        except TimeoutError:
            self._json(408, {"error": "Request timed out"})
        except Exception:
            LOG.exception("Unexpected request failure: route=%s", self.path)
            self._json(500, {"error": "Unexpected server error; inspect the server terminal"})


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Browse and synchronize local Git checkouts")
    parser.add_argument("--repositories", required=True, type=Path, help="Local JSON allowlist")
    parser.add_argument("--port", type=int, default=8089)
    parser.add_argument("--execution", choices=("direct", "camunda"), default="direct")
    parser.add_argument("--process-id", default="git-upstream-sync")
    parser.add_argument("--operate-url", default="http://127.0.0.1:8080/operate")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("Port must be between 1 and 65535")
    try:
        executor = None
        if args.execution == "camunda":
            from .camunda_web import CamundaExecutor
            executor = CamundaExecutor(args.process_id, args.operate_url)
        server = WebServer(load_repositories(args.repositories), args.port, executor)
    except (OSError, ValueError, ImportError) as error:
        parser.error(str(error))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    with server:
        print(f"Open http://127.0.0.1:{server.server_port} (Ctrl+C to stop)", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
