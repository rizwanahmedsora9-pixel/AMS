"""Signed GitHub push webhook for AMS on PythonAnywhere.

Mounted outside Flask by wsgi.py; see README.md for server setup.
Secrets and deployment logs stay in the ignored instance directory.
"""
import fcntl
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import subprocess

REPO_DIR = Path(__file__).resolve().parent
BRANCH = os.environ.get("AMS_DEPLOY_BRANCH", "main")
SECRET_FILE = Path(os.environ.get(
    "AMS_DEPLOY_SECRET_FILE", str(REPO_DIR / "instance/deploy_secret.txt")
)).expanduser()
# Explicit selection avoids reloading a different app on a shared account.
WSGI_FILE = os.environ.get("AMS_WSGI_FILE", "")
MAX_BODY = 2 * 1024 * 1024
logger = logging.getLogger(__name__)


def _secret():
    try:
        return SECRET_FILE.read_text().strip()
    except OSError:
        return ""


def _run_git(*args):
    return subprocess.check_output(
        ["git", *args], cwd=REPO_DIR, stderr=subprocess.STDOUT,
        timeout=20, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    ).decode(errors="replace").strip()


def _deploy():
    """Serialize pulls and refuse wrong-branch or dirty server checkouts."""
    if not WSGI_FILE or not Path(WSGI_FILE).is_file():
        return "503 Service Unavailable", "Configure AMS_WSGI_FILE before deploying.\n"
    lock_path = REPO_DIR / "instance/deploy.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "409 Conflict", "Another deployment is running; retry delivery.\n"
        try:
            if _run_git("branch", "--show-current") != BRANCH:
                return "409 Conflict", "Server checkout is on the wrong branch.\n"
            if _run_git("status", "--porcelain"):
                return "409 Conflict", "Server checkout has local changes; resolve them first.\n"
            _run_git("pull", "--ff-only", "origin", BRANCH)
            commit = _run_git("rev-parse", "--short", "HEAD")
            os.utime(WSGI_FILE, None)
            logger.info("AMS deployed %s; WSGI reload requested", commit)
            return "200 OK", f"Deployed {commit}; WSGI reload requested.\n"
        except (OSError, subprocess.SubprocessError):
            logger.exception("AMS deploy failed (code may have pulled before reload failed)")
            return "500 Internal Server Error", "Deployment failed; check the server error log.\n"


def _respond(start_response, status, text):
    body = text.encode("utf-8")
    start_response(status, [("Content-Type", "text/plain; charset=utf-8"),
                            ("Content-Length", str(len(body))),
                            ("Cache-Control", "no-store")])
    return [body]


def application(environ, start_response):
    """Validate signatures over raw bytes before inspecting event contents."""
    def respond(status, text):
        return _respond(start_response, status, text)

    method = environ.get("REQUEST_METHOD", "GET").upper()
    if method == "GET":
        # Do not expose paths, Git output, or deployment logs publicly.
        return respond("200 OK", "AMS deploy hook is reachable.\n")
    if method != "POST":
        return respond("405 Method Not Allowed", "Use GET or POST.\n")
    secret = _secret()
    if not secret:
        return respond("503 Service Unavailable", "Deployment secret is not configured.\n")
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except (ValueError, TypeError):
        return respond("400 Bad Request", "Invalid content length.\n")
    if length <= 0:
        return respond("400 Bad Request", "Empty or invalid body length.\n")
    if length > MAX_BODY:
        return respond("413 Payload Too Large", "Payload too large.\n")
    body = environ["wsgi.input"].read(length)
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    signature = environ.get("HTTP_X_HUB_SIGNATURE_256", "")
    if not hmac.compare_digest(expected.encode(), signature.encode()):
        return respond("401 Unauthorized", "Invalid signature.\n")
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return respond("400 Bad Request", "Invalid JSON.\n")
    if not isinstance(payload, dict):
        return respond("400 Bad Request", "Expected a JSON object.\n")
    event = environ.get("HTTP_X_GITHUB_EVENT", "")
    if event == "ping":
        return respond("200 OK", "pong\n")
    if event != "push" or payload.get("deleted") or payload.get("ref") != f"refs/heads/{BRANCH}":
        return respond("200 OK", "Ignored event.\n")
    status, text = _deploy()
    return respond(status, text)


def wrap_application(app):
    """Intercept only deployment URLs; leave all Flask CSRF checks intact."""
    def dispatch(environ, start_response):
        if environ.get("PATH_INFO") in {"/deploy", "/deploy/health", "/deploy/status"}:
            return application(environ, start_response)
        return app(environ, start_response)
    return dispatch
