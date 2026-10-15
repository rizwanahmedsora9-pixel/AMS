"""Signed GitHub push webhook for AMS on PythonAnywhere.

Mounted outside Flask by wsgi.py; see README.md for server setup.
Secrets and deployment logs stay in the ignored instance directory.

Delivery contract (kept deliberately boring for GitHub's Recent Deliveries):
- Every signed request is acknowledged in milliseconds. GitHub marks a
  delivery failed when the response takes more than ~10 seconds, so the
  pull + reload never runs inside the request.
- Push payloads are accepted up to MAX_BODY (32 MB by default). GitHub push
  events carry up to 20 commits with complete file lists plus the duplicated
  head commit; bulk imports and patch stacks blow well past the old 2 MB cap
  and were being refused with 413 before the ref was even inspected, so even
  pushes the hook would ignore showed up as failed deliveries.
- The HMAC-SHA256 signature is always verified over the raw bytes before the
  body is parsed or any deploy work is scheduled.
"""
import fcntl
import hashlib
import hmac
import json
import logging
import os
import threading
import time
from pathlib import Path
import subprocess

REPO_DIR = Path(__file__).resolve().parent
BRANCH = os.environ.get("AMS_DEPLOY_BRANCH", "main")
SECRET_FILE = Path(os.environ.get(
    "AMS_DEPLOY_SECRET_FILE", str(REPO_DIR / "instance/deploy_secret.txt")
)).expanduser()
# Explicit selection avoids reloading a different app on a shared account.
WSGI_FILE = os.environ.get("AMS_WSGI_FILE", "")
# DoS guard, not a business rule: real GitHub push payloads reach tens of MB
# (20 commits x thousands of changed paths x duplicated head commit), so the
# cap sits above anything GitHub sends and only stops unbounded bodies.
MAX_BODY = int(os.environ.get("AMS_DEPLOY_MAX_BODY", 32 * 1024 * 1024))
CHUNK = 64 * 1024
STATUS_FILE = Path(os.environ.get(
    "AMS_DEPLOY_STATUS_FILE", str(REPO_DIR / "instance/deploy_status.json")
)).expanduser()
GIT_TIMEOUT = 20          # local plumbing commands
GIT_TIMEOUT_PULL = 180    # network pull; the request is already answered by then
logger = logging.getLogger(__name__)

# Background deploy worker: one pending flag coalesces a burst of pushes (the
# last pull always lands the newest commit), and _running closes the
# lost-wakeup window between a worker draining the flag and exiting.
_pending = threading.Event()
_worker = None
_running = False
_worker_guard = threading.Lock()


def _secret():
    try:
        return SECRET_FILE.read_text().strip()
    except OSError:
        return ""


def _run_git(*args, timeout=GIT_TIMEOUT):
    return subprocess.check_output(
        ["git", *args], cwd=REPO_DIR, stderr=subprocess.STDOUT,
        timeout=timeout, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
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
            return "409 Conflict", "Another deployment is running.\n"
        try:
            if _run_git("branch", "--show-current") != BRANCH:
                return "409 Conflict", "Server checkout is on the wrong branch.\n"
            if _run_git("status", "--porcelain"):
                return "409 Conflict", "Server checkout has local changes; resolve them first.\n"
            _run_git("pull", "--ff-only", "origin", BRANCH, timeout=GIT_TIMEOUT_PULL)
            commit = _run_git("rev-parse", "--short", "HEAD")
            os.utime(WSGI_FILE, None)
            logger.info("AMS deployed %s; WSGI reload requested", commit)
            return "200 OK", f"Deployed {commit}; WSGI reload requested.\n"
        except (OSError, subprocess.SubprocessError):
            logger.exception("AMS deploy failed (code may have pulled before reload failed)")
            return "500 Internal Server Error", "Deployment failed; check the server error log.\n"


def _write_status(status, text):
    """Record the last deploy outcome for GET /deploy/status (no paths/secrets)."""
    try:
        STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATUS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "status": status,
            "detail": text.strip(),
            "when": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }))
        os.replace(tmp, STATUS_FILE)
    except OSError:
        logger.warning("AMS deploy status could not be written", exc_info=True)


def read_status():
    try:
        data = json.loads(STATUS_FILE.read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _deploy_worker():
    global _running
    while True:
        with _worker_guard:
            if not _pending.is_set():
                _running = False
                return
            _pending.clear()
        status, text = _deploy()
        if status.startswith("409") and "Another deployment" in text:
            # A pull in another worker process holds the lock; wait, then try
            # once more (any newer push has already set the pending flag).
            time.sleep(3)
            if not _pending.is_set():
                status, text = _deploy()
        _write_status(status, text)


def _queue_deploy():
    """Acknowledge now, deploy on a worker thread. Raises only if the thread
    cannot be started (the caller answers 500 in that case)."""
    global _worker, _running
    with _worker_guard:
        _pending.set()
        if not _running:
            _running = True
            _worker = threading.Thread(
                target=_deploy_worker, name="ams-deploy", daemon=True)
            _worker.start()


def join_deploy(timeout=None):
    """Wait for the current deploy worker (tests and manual checks)."""
    with _worker_guard:
        thread = _worker
    if thread is not None:
        thread.join(timeout=timeout)
        return not thread.is_alive()
    return True


def _read_body(environ):
    """Return ``(body, error)``; exactly one is None.

    The signature is computed over these raw bytes, so the body is read
    verbatim. A declared CONTENT_LENGTH is honoured and cross-checked; a
    missing length (chunked transfer) is read to EOF under the same cap.
    """
    stream = environ.get("wsgi.input")
    raw_len = environ.get("CONTENT_LENGTH")
    if raw_len not in (None, ""):
        try:
            length = int(raw_len)
        except (TypeError, ValueError):
            return None, ("400 Bad Request", "Invalid content length.\n")
        if length < 0:
            return None, ("400 Bad Request", "Invalid content length.\n")
        if length == 0:
            return None, ("400 Bad Request", "Empty or invalid body length.\n")
        if length > MAX_BODY:
            return None, ("413 Payload Too Large", "Payload too large.\n")
        body = stream.read(length) if stream is not None else b""
        if len(body) != length:
            return None, ("400 Bad Request", "Empty or invalid body length.\n")
        return body, None
    chunks = []
    total = 0
    if stream is not None:
        while True:
            chunk = stream.read(CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_BODY:
                return None, ("413 Payload Too Large", "Payload too large.\n")
            chunks.append(chunk)
    body = b"".join(chunks)
    if not body:
        return None, ("400 Bad Request", "Empty or invalid body length.\n")
    return body, None


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
        if environ.get("PATH_INFO") == "/deploy/status":
            st = read_status()
            lines = ["AMS deploy hook is reachable."]
            if st:
                lines.append("last deploy: %s at %s" % (st.get("status", "?"), st.get("when", "?")))
                lines.append(str(st.get("detail", "")))
            else:
                lines.append("no deployment attempted yet")
            return respond("200 OK", "\n".join(lines).rstrip() + "\n")
        return respond("200 OK", "AMS deploy hook is reachable.\n")
    if method != "POST":
        return respond("405 Method Not Allowed", "Use GET or POST.\n")
    secret = _secret()
    if not secret:
        return respond("503 Service Unavailable", "Deployment secret is not configured.\n")
    body, error = _read_body(environ)
    if error is not None:
        return respond(*error)
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
    delivery = environ.get("HTTP_X_GITHUB_DELIVERY", "")
    try:
        _queue_deploy()
    except Exception:
        logger.exception("AMS deploy could not be queued (delivery %s)", delivery)
        return respond("500 Internal Server Error",
                       "Deployment could not be queued; check the server error log.\n")
    logger.info("AMS deploy queued for %s (delivery %s)", payload.get("ref"), delivery)
    return respond("200 OK", "Deploy queued; pulling and reloading in the background.\n")


def wrap_application(app):
    """Intercept only deployment URLs; leave all Flask CSRF checks intact."""
    def dispatch(environ, start_response):
        if environ.get("PATH_INFO") in {"/deploy", "/deploy/health", "/deploy/status"}:
            return application(environ, start_response)
        return app(environ, start_response)
    return dispatch
