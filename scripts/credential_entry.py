"""Local one-shot credential entry for `.env` values.

    python scripts/credential_entry.py

Why this exists: secrets should never be pasted into a chat conversation and
the operator should not have to hand-edit `.env`. This script starts a tiny
HTTP form bound to 127.0.0.1 only, writes the submitted values into the two
market-data credential lines of `.env`, and exits.

Safety properties:

* loopback only - nothing outside this machine can reach it;
* one-time random token in the URL, checked with a constant-time compare;
* accepts exactly one submission, then shuts itself down;
* values are never echoed in the response, never printed, never logged;
* `.env` is read with newline preservation, only the two target lines are
  rewritten, and the write is refused unless every other line is byte-identical
  before and after;
* input is validated against a strict character set so a value can never
  corrupt or inject lines into `.env`;
* nothing here touches git - `.env` is ignored by the repository.
"""
from __future__ import annotations

import html
import re
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
TARGETS = ("MARKET_DATA_API_KEY", "MARKET_DATA_API_SECRET")
VALUE = re.compile(r"^[A-Za-z0-9+/=_\-]{4,200}$")

TOKEN = secrets.token_urlsafe(24)
_server: HTTPServer | None = None


def set_line(text: str, name: str, value: str) -> str:
    pattern = re.compile(rf"(?m)^{re.escape(name)}(?P<sp>[ \t]*)=[^\r\n]*(?P<eol>\r?)")
    replaced, count = pattern.subn(
        lambda m: f"{name}{m.group('sp')}={value}{m.group('eol')}", text, count=1)
    if count != 1:
        raise ValueError(f"{name} was not found in .env")
    return replaced


def others_unchanged(before: str, after: str) -> bool:
    def strip(text: str) -> list[str]:
        return [line for line in text.splitlines()
                if line.split("=", 1)[0].strip() not in TARGETS]
    return strip(before) == strip(after)


def save(key_id: str, secret: str) -> tuple[bool, str]:
    if not VALUE.match(key_id) or not VALUE.match(secret):
        return False, "Values may only contain letters, digits and + / = _ - (4-200 characters)."
    if not ENV.exists():
        return False, ".env does not exist next to the repository root."
    with ENV.open("r", encoding="utf-8", newline="") as handle:
        original = handle.read()
    try:
        updated = set_line(set_line(original, TARGETS[0], key_id),
                           TARGETS[1], secret)
    except ValueError as error:
        return False, str(error)
    if not others_unchanged(original, updated):
        return False, "Refused: the change would have touched other lines."
    with ENV.open("w", encoding="utf-8", newline="") as handle:
        handle.write(updated)
    # Report only lengths - never the values themselves.
    return True, (f"Saved. {TARGETS[0]}: {len(key_id)} characters, "
                  f"{TARGETS[1]}: {len(secret)} characters. "
                  f"No other line of .env changed.")


FORM = """<!doctype html>
<meta charset="utf-8">
<title>Alpha Trading - credential entry</title>
<style>
 body {{ font: 15px/1.5 system-ui, sans-serif; background:#0a1018; color:#e8edf5;
        display:flex; min-height:100vh; align-items:center; justify-content:center; margin:0; }}
 main {{ background:#101a26; border:1px solid #25364a; border-radius:10px;
         padding:28px 30px; width:min(440px, 92vw); }}
 h1 {{ font-size:16px; margin:0 0 4px; }}
 p  {{ color:#8b99aa; font-size:13px; margin:0 0 18px; }}
 label {{ display:block; font-size:12px; color:#9fb0c3; margin:14px 0 5px; }}
 input {{ width:100%; box-sizing:border-box; background:#0b131e; color:#e8edf5;
          border:1px solid #2a394b; border-radius:6px; padding:10px 11px; font-size:14px; }}
 button {{ margin-top:20px; width:100%; background:#14253a; color:#dce4ee;
           border:1px solid #2f5e8f; border-radius:6px; padding:11px; font-size:14px; cursor:pointer; }}
 button:hover {{ border-color:#4bd6a3; }}
 small {{ display:block; margin-top:14px; color:#6f7f92; font-size:11px; }}
 .ok {{ color:#4bd6a3; font-weight:600; }}
 .bad {{ color:#dd7474; }}
</style>
<main>
 <h1>Market-data credential entry</h1>
 <p>Loopback only, one submission, then this window closes. Values go straight
    into <code>.env</code> and are never shown, logged, or committed.</p>
 <form method="post" action="/save">
  <input type="hidden" name="token" value="{token}">
  <label for="key">Alpaca API Key ID</label>
  <input id="key" name="key" autocomplete="off" autocapitalize="off" spellcheck="false" required>
  <label for="secret">Alpaca API Secret</label>
  <input id="secret" name="secret" type="password" autocomplete="off" required>
  <button type="submit">Save securely</button>
 </form>
 <small>Broker credentials stay empty and live execution stays disabled - these
        two fields are market-data only.</small>
</main>
"""

RESULT = """<!doctype html>
<meta charset="utf-8">
<title>Alpha Trading - result</title>
<style>
 body {{ font: 15px/1.5 system-ui, sans-serif; background:#0a1018; color:#e8edf5;
        display:flex; min-height:100vh; align-items:center; justify-content:center; margin:0; }}
 main {{ background:#101a26; border:1px solid #25364a; border-radius:10px;
         padding:28px 30px; width:min(440px, 92vw); }}
 .ok {{ color:#4bd6a3; }} .bad {{ color:#dd7474; }}
</style>
<main><p class="{cls}">{message}</p><p style="color:#8b99aa;font-size:13px">
You can close this tab.</p></main>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # never write request lines anywhere
        return

    def _send(self, status: int, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        if self.path.startswith("/?" ) or self.path == "/" or self.path.startswith("/#"):
            if not secrets.compare_digest(
                    parse_qs(self.path.split("?", 1)[-1]).get("t", [""])[0], TOKEN):
                self._send(403, RESULT.format(cls="bad", message="Bad or expired link."))
                return
            self._send(200, FORM.format(token=TOKEN))
        else:
            self._send(404, RESULT.format(cls="bad", message="Not found."))

    def do_POST(self) -> None:
        if self.path != "/save":
            self._send(404, RESULT.format(cls="bad", message="Not found."))
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length > 8192:
            self._send(413, RESULT.format(cls="bad", message="Request too large."))
            return
        fields = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        if not secrets.compare_digest(fields.get("token", [""])[0], TOKEN):
            self._send(403, RESULT.format(cls="bad", message="Bad token."))
            return
        ok, message = save(fields.get("key", [""])[0].strip(),
                          fields.get("secret", [""])[0].strip())
        self._send(200, RESULT.format(cls="ok" if ok else "bad",
                                     message=html.escape(message)))
        print(("SAVED  " if ok else "FAILED ") + message, flush=True)
        if ok:
            threading.Thread(target=_server.shutdown, daemon=True).start()


def main() -> int:
    global _server
    if not ENV.exists():
        print("FAILED .env not found at " + str(ENV))
        return 1
    _server = HTTPServer(("127.0.0.1", 0), Handler)
    port = _server.server_address[1]
    print(f"OPEN   http://127.0.0.1:{port}/?t={TOKEN}", flush=True)
    print("waiting for one submission (loopback only) ...", flush=True)
    try:
        _server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _server.server_close()
    print("closed", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
