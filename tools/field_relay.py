"""Phone demo relay: lets audience phones open the field app of the deployed Databricks App, which itself only admits signed-in users.

Runs on the presenter's laptop and forwards only the field app (the /field page, its own static files, Leaflet, icons) and the API it
calls (/api/field/*, the field report /api/fieldreport/meta and /submit, and the asset photo: /api/assets/search to tag the asset and
/api/assets/photo to upload it) to the deployed app, adding the presenter's Databricks login as a bearer token. Everything else gets 404,
so the tunnel in front of it (tools/field_demo.sh) exposes the field app and nothing more. Phones and the control room share the same
live state.
Stdlib only.
  APP_URL=https://<app>.databricksapps.com DATABRICKS_CONFIG_PROFILE=<cli-profile> python3 tools/field_relay.py [--port 8787]
  (or pass --app https://<app>.databricksapps.com --profile <cli-profile> instead of the two environment variables)
"""
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGES = re.compile(r"^/(field|static/field\.(js|css|webmanifest)|static/field-sw\.js|static/vendor/leaflet/[\w./-]+|static/icons/[\w.-]+)$")
API = re.compile(r"^/api/(field/[\w-]+|fieldreport/(meta|submit)|assets/(search|photo))$")
MAX_BODY = 256 * 1024                      # any request body, except a photo
PHOTO = "/api/assets/photo"
MAX_PHOTO_BODY = 12 * 1024 * 1024          # a photo upload: the app takes up to 8 MB of image, which is about 11 MB as base64 JSON
PASS = ("Content-Type", "Cache-Control", "ETag", "Last-Modified")


class Token:
    """The presenter's Databricks OAuth token from the CLI, refreshed five minutes before it expires."""

    def __init__(self, profile):
        self.profile, self.value, self.expires, self.lock = profile, None, 0.0, threading.Lock()

    def get(self):
        with self.lock:
            if not self.value or time.time() > self.expires - 300:
                cmd = ["databricks", "auth", "token", "-p", self.profile]
                try:   # the CLI v1.x file cache, else a login kept in the system keychain (the CLI's default store)
                    raw = subprocess.check_output(cmd, env=dict(os.environ, DATABRICKS_AUTH_STORAGE="plaintext"), text=True, stderr=subprocess.DEVNULL)
                except subprocess.CalledProcessError:
                    raw = subprocess.check_output(cmd, env={k: v for k, v in os.environ.items() if k != "DATABRICKS_AUTH_STORAGE"}, text=True)
                out = json.loads(raw)
                self.value = out["access_token"]
                try:
                    self.expires = dt.datetime.fromisoformat(out["expiry"].replace("Z", "+00:00")).timestamp()
                except (KeyError, ValueError):
                    self.expires = time.time() + 1800
            return self.value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):   # a redirect means the app wants a sign-in: report it rather than follow it
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def handler(app, token):
    class Relay(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _reply(self, code, body, ctype="text/plain; charset=utf-8", extra=()):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in extra:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _forward(self, allowed):
            path = self.path.split("?", 1)[0]
            n = int(self.headers.get("Content-Length") or 0)
            limit = MAX_PHOTO_BODY if path == PHOTO else MAX_BODY
            if ".." in path or not allowed(path) or n > limit:
                self.close_connection = True   # the unread body must not be taken for the next request
                return self._reply(404 if n <= limit else 413, b"Not found" if n <= limit else b"The request is too large.")
            body = self.rfile.read(n) if n else None
            req = urllib.request.Request(app + self.path, data=body, method=self.command,
                                         headers={"Authorization": f"Bearer {token.get()}", "User-Agent": "field-relay",
                                                  **({"Content-Type": self.headers["Content-Type"]} if self.headers.get("Content-Type") else {})})
            try:
                with OPENER.open(req, timeout=60) as r:   # a photo upload over a phone connection can take a while
                    code, headers, data = r.status, r.headers, r.read()
            except urllib.error.HTTPError as e:
                if 300 <= e.code < 400 or e.code in (401, 403):
                    return self._reply(502, b"The relay could not sign in to the app. Run: databricks auth login -p " + token.profile.encode())
                code, headers, data = e.code, e.headers, e.read()
            except (urllib.error.URLError, TimeoutError) as e:
                return self._reply(504, f"The app did not answer: {e}".encode())
            extra = [(k, headers[k]) for k in PASS if headers.get(k) and k != "Content-Type"]
            if path == "/static/field-sw.js":
                extra.append(("Service-Worker-Allowed", "/field"))   # the worker lives under /static but looks after /field
            self._reply(code, data, headers.get("Content-Type", "application/octet-stream"), extra)

        def do_GET(self):
            self._forward(lambda p: PAGES.match(p) or API.match(p))

        def do_POST(self):
            self._forward(API.match)

        def log_message(self, fmt, *args):
            print(f"{self.log_date_time_string()} {self.command} {self.path.split('?', 1)[0]} {args[1] if len(args) > 1 else ''}", flush=True)

    return Relay


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--profile", default=os.environ.get("DATABRICKS_CONFIG_PROFILE"), help="The Databricks CLI profile to sign in with (env: DATABRICKS_CONFIG_PROFILE).")
    ap.add_argument("--app", default=os.environ.get("APP_URL"), help="The URL of the deployed app (env: APP_URL).")
    a = ap.parse_args()
    if not a.app or not a.profile:
        ap.error("Set APP_URL and DATABRICKS_CONFIG_PROFILE, or pass --app and --profile.")
    token = Token(a.profile)
    token.get()   # fail fast if the presenter is not signed in
    print(f"field relay on http://127.0.0.1:{a.port}/field -> {a.app}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", a.port), handler(a.app.rstrip("/"), token)).serve_forever()


if __name__ == "__main__":
    main()
