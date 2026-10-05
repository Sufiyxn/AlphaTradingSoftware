import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import load_settings


class StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path not in ("/api/health", "/api/status"):
            self.send_error(404)
            return

        settings = load_settings()
        payload = {
            "status": "ok",
            "service": "alpha-trading-platform",
            "environment": settings.environment,
        }
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    settings = load_settings()
    server = ThreadingHTTPServer((settings.host, settings.port), StatusHandler)
    print(f"Status API listening at http://{settings.host}:{settings.port}/api/health")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
