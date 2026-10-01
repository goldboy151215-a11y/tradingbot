#!/usr/bin/env python3
"""Web Dashboard Server & Authenticated API.

Enforces:
- Mandatory HTTP Basic Authentication for all endpoints (no open HTTP buttons)
- Truthful telemetry strictly sourced from runtime_state.json
- Zero theater: no orbit-galaxy, no Dirigent, no backup-switcher, no T2 risk, no confluence %
- /start rejection on config mismatch
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

import requests

USER_DATA = "/root/ft_userdata/user_data"
if not os.path.exists(USER_DATA):
    USER_DATA = "/freqtrade/user_data"

CONFIG_PATH = os.path.join(USER_DATA, "config.json")
RUNTIME_STATE_PATH = os.path.join(USER_DATA, "runtime_state.json")
HTML_PATH = os.path.join(USER_DATA, "index.html")

sys.path.insert(0, "/root")
sys.path.insert(0, USER_DATA)

from generate_dashboard import generate_dashboard
from src.runtime_state import (
    RuntimeState,
    load_runtime_state,
    save_runtime_state,
    verify_config_sync,
)
from trade_analyzer import audit_single_trade, analyze_daily_opportunity

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [DashboardServer] %(message)s",
)
logger = logging.getLogger(__name__)

# Credentials from config.json
with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    config = json.load(f)

AUTH_USER = config.get("api_server", {}).get("username", "admin")
AUTH_PASS = config.get("api_server", {}).get("password", "mendy7218")
EXPECTED_AUTH = "Basic " + base64.b64encode(f"{AUTH_USER}:{AUTH_PASS}".encode("utf-8")).decode("utf-8")

FT_API_URL = "http://127.0.0.1:8080/api/v1"
FT_AUTH = (AUTH_USER, AUTH_PASS)


class AuthenticatedDashboardHandler(BaseHTTPRequestHandler):
    def _is_authenticated(self) -> bool:
        auth_header = self.headers.get("Authorization")
        if auth_header and auth_header.strip() == EXPECTED_AUTH.strip():
            return True
        cookie_header = self.headers.get("Cookie", "")
        if "dashboard_session=authenticated" in cookie_header:
            return True
        return False

    def _send_unauthorized(self, is_json: bool = False) -> None:
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Freqtrade Mission Control"')
        if is_json:
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(b'{"status":"error","error":"Unauthorized: Inloggen verplicht."}')
        else:
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Unauthorized: Inloggen verplicht. Geen open toegang.")

    def _set_headers(self, content_type="text/html", status_code=200, set_cookie=False) -> None:
        self.send_response(status_code)
        self.send_header("Content-type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        if set_cookie:
            self.send_header("Set-Cookie", "dashboard_session=authenticated; Path=/; SameSite=Lax")
        self.end_headers()

    def do_OPTIONS(self) -> None:
        self._set_headers(status_code=204)

    def do_HEAD(self) -> None:
        if not self._is_authenticated():
            self._send_unauthorized()
            return
        self._set_headers()

    def do_GET(self) -> None:
        # Mandatory Authentication for all requests
        if not self._is_authenticated():
            parsed = urlparse(self.path)
            is_json = parsed.path.startswith("/api/")
            self._send_unauthorized(is_json=is_json)
            return

        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html", "/dashboard.html"):
            try:
                generate_dashboard()
            except Exception as e:
                logger.error(f"Error regenerating dashboard: {e}")

            if os.path.exists(HTML_PATH):
                with open(HTML_PATH, "rb") as f:
                    content = f.read()
                self._set_headers("text/html; charset=utf-8", set_cookie=True)
                self.wfile.write(content)
            else:
                self._set_headers("text/plain", 404)
                self.wfile.write(b"Dashboard HTML niet gevonden.")

        elif path in ("/api/runtime_state", "/api/status", "/api/params"):
            state = load_runtime_state(RUNTIME_STATE_PATH)
            self._set_headers("application/json")
            self.wfile.write(json.dumps(state.to_dict()).encode("utf-8"))

        elif path == "/api/analytics/latest":
            data = audit_single_trade()
            self._set_headers("application/json")
            self.wfile.write(json.dumps(data).encode("utf-8"))

        elif path == "/api/analytics/daily":
            data = analyze_daily_opportunity()
            self._set_headers("application/json")
            self.wfile.write(json.dumps(data).encode("utf-8"))

        elif path == "/api/ping":
            self._set_headers("application/json")
            self.wfile.write(b'{"status":"pong"}')

        else:
            self._set_headers("text/plain", 404)
            self.wfile.write(b"Not Found")

    def do_POST(self) -> None:
        # Mandatory Authentication for all actions
        if not self._is_authenticated():
            self._send_unauthorized(is_json=True)
            return

        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/bot/start":
            state = load_runtime_state(RUNTIME_STATE_PATH)
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                ft_config = json.load(f)

            is_valid, mismatches = verify_config_sync(state, ft_config)
            if not is_valid:
                logger.error(f"Start rejected via Dashboard: {mismatches}")
                self._set_headers("application/json", 400)
                self.wfile.write(json.dumps({
                    "status": "error",
                    "error": "CONFIG MISMATCH. Bot start NIET tot dit gelijk is.",
                    "mismatches": mismatches
                }).encode("utf-8"))
                return

            try:
                r = requests.post(f"{FT_API_URL}/start", auth=FT_AUTH, headers={"Connection": "close"}, timeout=(3, 5))
                self._set_headers("application/json")
                self.wfile.write(json.dumps({
                    "status": "success",
                    "message": "Trading loop succesvol gestart na config verificatie."
                }).encode("utf-8"))
            except Exception as exc:
                self._set_headers("application/json", 500)
                self.wfile.write(json.dumps({"status": "error", "error": str(exc)}).encode("utf-8"))

        elif path == "/api/bot/stop":
            try:
                r = requests.post(f"{FT_API_URL}/stop", auth=FT_AUTH, headers={"Connection": "close"}, timeout=(3, 5))
                self._set_headers("application/json")
                self.wfile.write(json.dumps({
                    "status": "success",
                    "message": "Trading loop gepauzeerd (/stop)."
                }).encode("utf-8"))
            except Exception as exc:
                self._set_headers("application/json", 500)
                self.wfile.write(json.dumps({"status": "error", "error": str(exc)}).encode("utf-8"))

        elif path == "/api/bot/kill":
            try:
                r = requests.post(f"{FT_API_URL}/forceexit", auth=FT_AUTH, json={}, headers={"Connection": "close"}, timeout=(3, 5))
                self._set_headers("application/json")
                self.wfile.write(json.dumps({
                    "status": "success",
                    "message": "Noodstop uitgevoerd (/kill). Posities worden gesloten."
                }).encode("utf-8"))
            except Exception as exc:
                self._set_headers("application/json", 500)
                self.wfile.write(json.dumps({"status": "error", "error": str(exc)}).encode("utf-8"))

        elif path == "/api/bot/reload":
            try:
                r = requests.post(f"{FT_API_URL}/reload_config", auth=FT_AUTH, headers={"Connection": "close"}, timeout=(3, 5))
                generate_dashboard()
                self._set_headers("application/json")
                self.wfile.write(json.dumps({
                    "status": "success",
                    "message": "Config herladen."
                }).encode("utf-8"))
            except Exception as exc:
                self._set_headers("application/json", 500)
                self.wfile.write(json.dumps({"status": "error", "error": str(exc)}).encode("utf-8"))

        else:
            self._set_headers("text/plain", 404)
            self.wfile.write(b"Not Found")


def run(port: int = 80) -> None:
    server_address = ("0.0.0.0", port)
    try:
        httpd = HTTPServer(server_address, AuthenticatedDashboardHandler)
        logger.info(f"Dashboard Web Server met Auth gestart op http://0.0.0.0:{port}")
        httpd.serve_forever()
    except PermissionError:
        logger.warning(f"Geen permissies voor poort {port}, fallback naar 8088")
        httpd = HTTPServer(("0.0.0.0", 8088), AuthenticatedDashboardHandler)
        logger.info("Dashboard Web Server met Auth gestart op http://0.0.0.0:8088")
        httpd.serve_forever()


if __name__ == "__main__":
    port = 80
    if len(sys.argv) > 1:
        port = int(sys.argv[1])
    run(port)
