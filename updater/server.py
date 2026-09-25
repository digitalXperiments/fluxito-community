"""Fluxito updater sidecar.

A tiny stdlib HTTP server that performs the privileged image pull + container
recreate. Reachable only on the internal Docker network (no host port, never
proxied by nginx) and authenticated with a shared bearer token.

Endpoints:
  POST /update   {"version": "1.0.5"}   -> begin update (async, returns 202)
  GET  /status                          -> current job status

Job status is written to a JSON file on a shared volume so it survives the app
container restart that the update itself causes.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ.get("UPDATER_TOKEN", "")
COMPOSE_FILE = os.environ.get("COMPOSE_FILE", "/compose/docker-compose.yml")
ENV_FILE = os.environ.get("ENV_FILE", "/compose/.env")
STATE_FILE = os.environ.get("STATE_FILE", "/state/update.json")
APP_SERVICE = os.environ.get("APP_SERVICE", "app")
HEALTH_TIMEOUT = int(os.environ.get("HEALTH_TIMEOUT", "180"))
PORT = int(os.environ.get("UPDATER_PORT", "9000"))

_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_lock = threading.Lock()


def is_valid_version(version: str) -> bool:
    """Strict MAJOR.MINOR.PATCH only — blocks shell-injection via the version field."""
    return bool(_VERSION_RE.match(version or ""))


def upsert_env_var(path: str, key: str, value: str) -> None:
    """Set KEY=value in an env file, replacing any existing line, leaving others intact."""
    line = f"{key}={value}"
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        lines = []
    out, replaced = [], False
    for existing in lines:
        if existing.startswith(f"{key}="):
            out.append(line)
            replaced = True
        else:
            out.append(existing)
    if not replaced:
        out.append(line)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")


def write_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    os.replace(tmp, STATE_FILE)


def read_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"status": "idle"}


def _compose(*args: str, version: str | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if version is not None:
        env["FLUXITO_VERSION"] = version
    return subprocess.run(  # noqa: S603
        ["docker", "compose", "-f", COMPOSE_FILE, *args],  # noqa: S607
        capture_output=True, text=True, env=env, check=False,
    )


def _app_healthy() -> bool:
    res = subprocess.run(  # noqa: S603
        ["docker", "inspect", "-f", "{{.State.Health.Status}}", "fluxito-app"],  # noqa: S607
        capture_output=True, text=True, check=False,
    )
    return res.stdout.strip() == "healthy"


def _wait_healthy(timeout: int) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _app_healthy():
            return True
        time.sleep(3)
    return False


def run_update(target: str, previous: str) -> None:
    """Pull -> recreate -> health-check -> rollback on failure. Records state throughout."""
    write_state({"status": "pulling", "target": target, "previous": previous})
    upsert_env_var(ENV_FILE, "FLUXITO_VERSION", target)
    pull = _compose("pull", APP_SERVICE, version=target)
    if pull.returncode != 0:
        # Nothing was recreated; revert the env pin so .env matches the running container.
        if previous:
            upsert_env_var(ENV_FILE, "FLUXITO_VERSION", previous)
        write_state({"status": "failed", "stage": "pull", "target": target,
                     "error": pull.stderr[-2000:]})
        return
    write_state({"status": "recreating", "target": target, "previous": previous})
    up = _compose("up", "-d", APP_SERVICE, version=target)
    if up.returncode != 0:
        _rollback(previous, "recreate", up.stderr[-2000:])
        return
    write_state({"status": "verifying", "target": target, "previous": previous})
    if _wait_healthy(HEALTH_TIMEOUT):
        write_state({"status": "success", "current": target, "previous": previous})
    else:
        _rollback(previous, "healthcheck", "new container did not become healthy")


def _rollback(previous: str, stage: str, error: str) -> None:
    write_state({"status": "rolling_back", "previous": previous, "stage": stage})
    upsert_env_var(ENV_FILE, "FLUXITO_VERSION", previous)
    _compose("up", "-d", APP_SERVICE, version=previous)
    write_state({"status": "failed", "stage": stage, "error": error,
                 "current": previous, "rolled_back": True})


class Handler(BaseHTTPRequestHandler):
    def _auth_ok(self) -> bool:
        return TOKEN and self.headers.get("Authorization") == f"Bearer {TOKEN}"

    def _send(self, code: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # stdlib naming convention (uppercase verb)
        if self.path != "/status":
            return self._send(404, {"error": "not found"})
        if not self._auth_ok():
            return self._send(401, {"error": "unauthorized"})
        self._send(200, read_state())

    def do_POST(self):  # stdlib naming convention (uppercase verb)
        if self.path != "/update":
            return self._send(404, {"error": "not found"})
        if not self._auth_ok():
            return self._send(401, {"error": "unauthorized"})
        length = min(int(self.headers.get("Content-Length", "0") or "0"), 1_048_576)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "invalid json"})
        target = str(body.get("version", "")).lstrip("vV")
        previous = str(body.get("previous", "")).lstrip("vV")
        if not is_valid_version(target):
            return self._send(400, {"error": "invalid version"})
        if previous and not is_valid_version(previous):
            return self._send(400, {"error": "invalid previous version"})
        if not _lock.acquire(blocking=False):
            return self._send(409, {"error": "update already in progress"})
        started = False
        try:
            threading.Thread(
                target=lambda: self._guarded_run(target, previous), daemon=True
            ).start()
            started = True
        finally:
            if not started:
                _lock.release()  # never started -> _guarded_run won't run -> release here
        self._send(202, {"status": "accepted", "target": target})

    def _guarded_run(self, target: str, previous: str) -> None:
        try:
            run_update(target, previous)
        finally:
            _lock.release()

    def log_message(self, *args):  # silence default stderr logging
        pass


if __name__ == "__main__":
    if not TOKEN:
        print(
            "WARNING: UPDATER_TOKEN is not set — the updater will reject all requests. "
            "Set UPDATER_TOKEN to enable in-app self-update.",
            file=sys.stderr,
        )
    write_state({"status": "idle"})
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()  # noqa: S104
