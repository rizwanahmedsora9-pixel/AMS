#!/usr/bin/env python3
"""AMS PythonAnywhere deployment installer — one file, standard library only.

Run it directly on the PythonAnywhere account:

    python3 setup_Deploy.py

What it does (never more than it can verify):

  1. Inspects the target directory *before* changing anything and prints a plan.
  2. Installs or fast-forward-updates the AMS Git checkout in place, including
     the home-directory bootstrap (no ``AMS`` subfolder) used in production.
  3. Creates/reuses the virtualenv and installs ``requirements.txt`` into it.
  4. Ensures the canonical signed webhook hook and ``instance/deploy_secret.txt``.
  5. Adds one managed block to the exact PythonAnywhere WSGI file so the hook is
     mounted once (``AMS_WSGI_FILE``, ``AMS_DEPLOY_BRANCH``, ``SQLITE_JOURNAL_MODE``,
     ``AMS_HTTPS``).
  6. Optionally configures the Web tab and GitHub webhook through authenticated
     APIs when a token is available; otherwise it prints the exact manual steps.
  7. Runs staged validation (files, venv, WSGI, HTTP, ``/deploy/health``, signed
     ping, webhook, push->pull->reload) and says plainly what is *not* verified.

Safety rules built in (see README "Deployment"):

  * Never ``git reset --hard``, ``git clean``, ``git stash``, or force-checkout.
  * Never overwrite tracked application files and never modify a database.
  * Never import the Flask application (a "check" could create data or migrate).
  * Secrets are written with mode 0600, preserved on reruns, never logged, and
    never printed unless you ask with ``--show-secret``.
  * Material changes are confirmed first; ``--dry-run`` changes nothing.
  * Setup runs under a lock; a rerun resumes/re-checks instead of starting over.

Exit codes: 0 = everything required is done, 2 = done but manual steps remain,
1 = blocked/failed.  "Complete" is only printed when no required step remains.
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import datetime as _dt
import fcntl
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Sequence

VERSION = "1.0.0"

DEFAULT_TARGET = "/home/rehmanahmed"
DEFAULT_DOMAIN = "rehmanahmed.pythonanywhere.com"
DEFAULT_REPO_URL = "https://github.com/rizwanahmedsora9-pixel/AMS.git"
DEFAULT_BRANCH = "main"
DEFAULT_VENV_NAME = ".venv"

INSTALLER_NAME = "setup_Deploy.py"
HOOK_FILE = "deploy_hook.py"
WSGI_ENTRY = "wsgi.py"
SECRET_REL = os.path.join("instance", "deploy_secret.txt")
SETUP_REL = os.path.join("instance", "setup")
EXPOSED_SECRET_REL = "deploy_secret.txt"

MANAGED_BEGIN = "# >>> AMS deployment setup (managed by setup_Deploy.py) >>>"
MANAGED_END = "# <<< AMS deployment setup (managed by setup_Deploy.py) <<<"

REQUIRED_PATHS = ("wsgi.py", "deploy_hook.py", "requirements.txt", "app", "models", "templates", "static")
# Markers that the deployed hook must provide.  Required markers mean the file is
# not the supported hook; recommended markers are reported as warnings so a
# future refactor does not hard-fail an otherwise working deployment.
HOOK_REQUIRED_MARKERS = ("wrap_application", "AMS_WSGI_FILE", "--ff-only")
HOOK_RECOMMENDED_MARKERS = ("HTTP_X_HUB_SIGNATURE_256", "hmac", "AMS_DEPLOY_BRANCH", "flock")
# How the repository's wsgi.py must mount the hook.
HOOK_MOUNT_MARKERS = ("from deploy_hook import wrap_application", "wrap_application(")
# Imports a hand-written PythonAnywhere WSGI file may contain for this project.
WSGI_WRAP_MARKERS = HOOK_MOUNT_MARKERS + ("from wsgi import application",
                                          "from main import app as application")

TRANSIENT_MARKERS = (
    "temporary failure in name resolution",
    "connection reset",
    "connection aborted",
    "connection refused",
    "connection timed out",
    "read timed out",
    "timed out",
    "timeout",
    "network is unreachable",
    "tls connection was non-properly terminated",
    "eof occurred in violation of protocol",
    "ssl error",
    "502 bad gateway",
    "503 service unavailable",
    "504 gateway time-out",
    "500 internal server error",
    "remote end hung up unexpectedly",
    "early eof",
    "the read operation timed out",
)

DEPLOY_PATH = "/deploy"
HEALTH_PATH = "/deploy/health"
PING_BODY = json.dumps({"zen": "AMS setup signature self-check", "hook_id": 0}, sort_keys=True).encode("utf-8")

STAGE_ORDER = ("preflight", "git", "python", "secret", "wsgi", "pythonanywhere", "webhook", "validation")

PASS, FAIL, MANUAL, SKIP, WARN, INFO = "PASS", "FAIL", "MANUAL", "SKIP", "WARN", "INFO"


# --------------------------------------------------------------------------- #
# Small generic helpers
# --------------------------------------------------------------------------- #
def which(name: str) -> "str | None":
    """PATH lookup indirection (tests neutralise it so no real tool is used)."""
    return shutil.which(name)


def now_stamp() -> str:
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def now_iso() -> str:
    return _dt.datetime.now().replace(microsecond=0).isoformat()


def tail(text: str, lines: int = 12, width: int = 400) -> str:
    """Last non-empty lines of command output, clipped for readable reports."""
    parts = [line.strip() for line in (text or "").splitlines() if line.strip()]
    clipped = [part[:width] for part in parts[-lines:]]
    return "\n".join(clipped)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint(value: str) -> str:
    """Public, non-reversible identifier for a secret (safe to print/log)."""
    return "sha256:" + sha256_bytes(value.encode("utf-8"))[:12]


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def file_mode(path: Path) -> int:
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return 0


def ensure_dir(path: Path, mode: int = 0o700) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def atomic_write(path: Path, data: "str | bytes", mode: int = 0o600) -> None:
    """Write via a private temp file + rename so readers never see a half file."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (".%s.tmp.%s" % (path.name, secrets.token_hex(4)))
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(tmp), str(path))
        os.chmod(str(path), mode)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def redact_url(url: str) -> str:
    """Drop credentials embedded in a URL (never log or store them)."""
    try:
        split = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    if "@" not in (split.netloc or ""):
        return url
    host = split.netloc.rsplit("@", 1)[1]
    return urllib.parse.urlunsplit((split.scheme, "***@" + host, split.path, split.query, split.fragment))


def normalize_repo_url(url: str) -> str:
    """Canonical ``host/owner/repo`` so https/ssh/trailing-'.git' all compare equal."""
    text = (url or "").strip()
    if not text:
        return ""
    if text.startswith("git@") and ":" in text:
        host, _, path = text[4:].partition(":")
        text = "https://%s/%s" % (host, path)
    elif text.startswith("ssh://"):
        split = urllib.parse.urlsplit(text)
        text = "https://%s%s" % (split.hostname or "", split.path)
    if "://" not in text:
        text = "https://" + text
    split = urllib.parse.urlsplit(text)
    host = (split.hostname or "").lower()
    path = (split.path or "").strip("/")
    if path.lower().endswith(".git"):
        path = path[:-4]
    return ("%s/%s" % (host, path)).rstrip("/").lower()


def repo_owner_name(url: str) -> str:
    canonical = normalize_repo_url(url)
    parts = canonical.split("/")
    return "/".join(parts[-2:]) if len(parts) >= 2 else canonical


def derive_wsgi_path(var_www: Path, domain: str) -> Path:
    """PythonAnywhere names each web app's WSGI file after its domain."""
    safe = re.sub(r"[^A-Za-z0-9]+", "_", domain.strip().lower()).strip("_")
    return var_www / ("%s_wsgi.py" % safe)


def python_version_tuple(text: str) -> "tuple[int, ...]":
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    if not match:
        return ()
    return tuple(int(part) for part in match.groups() if part is not None)


def version_string(major_minor: "tuple[int, ...]") -> str:
    return ".".join(str(part) for part in major_minor)


def is_secret_value_valid(value: str) -> bool:
    """Accept a webhook secret generated by PythonAnywhere/GitHub or by us."""
    value = (value or "").strip()
    if len(value) < 32 or len(value) > 512:
        return False
    if not re.fullmatch(r"[!-~]+", value):  # printable ASCII, no spaces
        return False
    return True


def normalize_wsgi_source(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


# --------------------------------------------------------------------------- #
# Redaction: applied to every log line and to command output we print
# --------------------------------------------------------------------------- #
class Redactor:
    """Replace known secrets and credential-shaped strings with a marker."""

    _PATTERNS = (
        re.compile(r"(?i)(authorization\s*[:=]\s*)(?:token|bearer)\s+\S+"),
        re.compile(r"(?i)(x-hub-signature-256\s*[:=]\s*)\S+"),
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"),
        re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
        re.compile(r"(?i)\b(api[_-]?token|access[_-]?token|client[_-]?secret)\b\s*[=:]\s*\S+"),
        re.compile(r"([a-z][a-z0-9+.-]*://)[^/@\s]+:[^/@\s]+@", re.IGNORECASE),
    )

    def __init__(self) -> None:
        self._values: "set[str]" = set()

    def add(self, value: "str | None") -> None:
        if value and len(str(value)) >= 8:
            self._values.add(str(value))

    def redact(self, text: Any) -> str:
        if text is None:
            return ""
        result = str(text)
        for value in sorted(self._values, key=len, reverse=True):
            if value and value in result:
                result = result.replace(value, "***REDACTED***")
        for pattern in self._PATTERNS:
            if pattern.groups:
                result = pattern.sub(lambda m: m.group(1) + "***REDACTED***", result)
            else:
                result = pattern.sub("***REDACTED***", result)
        return result


# --------------------------------------------------------------------------- #
# Records shared by the stages
# --------------------------------------------------------------------------- #
@dataclasses.dataclass
class Result:
    """Outcome of an external command."""

    argv: "list[str]"
    rc: int = 0
    out: str = ""
    err: str = ""
    timed_out: bool = False
    skipped: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.rc == 0 and not self.timed_out and not self.error

    @property
    def combined(self) -> str:
        return (self.out or "") + (("\n" + self.err) if self.err else "")

    def brief(self) -> str:
        return tail(self.combined) or "(no output)"


@dataclasses.dataclass
class HttpResponse:
    status: int = 0
    body: str = ""
    headers: "dict[str, str]" = dataclasses.field(default_factory=dict)
    url: str = ""
    error: str = ""
    skipped: bool = False

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def brief(self) -> str:
        body = " ".join((self.body or "").split())[:160]
        if self.error:
            return "error: %s" % self.error
        return "%s %s" % (self.status, body)

    def json(self) -> Any:
        return json.loads(self.body)


@dataclasses.dataclass
class Check:
    stage: str
    name: str
    status: str
    detail: str = ""
    fix: str = ""
    required: bool = True


@dataclasses.dataclass
class PlatformInfo:
    is_pythonanywhere: bool
    home: Path
    user: str
    var_www: Path
    interpreters: "list[tuple[tuple[int, ...], str, str]]" = dataclasses.field(default_factory=list)
    pythonanywhere_domain: str = ""
    os_name: str = os.name


def _interpreter_version(path: str) -> "tuple[int, ...]":
    try:
        proc = subprocess.run([path, "-c", "import sys;print('%d.%d.%d' % sys.version_info[:3])"],
                              capture_output=True, text=True, timeout=20)
        if proc.returncode == 0:
            return python_version_tuple(proc.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return ()


def detect_platform(env: "dict[str, str] | None" = None, var_www: Path = Path("/var/www")) -> PlatformInfo:
    env = dict(os.environ if env is None else env)
    home = Path(env.get("HOME") or Path.home()).expanduser()
    try:
        user = env.get("USER") or env.get("LOGNAME") or getpass.getuser()
    except Exception:  # pragma: no cover - exotic environments
        user = "unknown"
    pa_domain = env.get("PYTHONANYWHERE_DOMAIN") or env.get("PYTHONANYWHERE_SITE") or ""
    is_pa = bool(pa_domain) or "pythonanywhere" in socket.gethostname().lower()
    candidates = ["python3.13", "python3.12", "python3.11", "python3.10", "python3.9", "python3.8",
                  "python3", "python"]
    interpreters: "list[tuple[tuple[int, ...], str, str]]" = []
    seen: "set[str]" = set()
    for name in candidates:
        path = which(name)
        if not path:
            continue
        resolved = os.path.realpath(path)
        if resolved in seen:
            continue
        version = _interpreter_version(path)
        if not version:
            continue
        seen.add(resolved)
        interpreters.append((version, path, resolved))
    interpreters.sort(key=lambda item: item[0], reverse=True)
    return PlatformInfo(is_pythonanywhere=is_pa, home=home, user=user or "unknown",
                        var_www=var_www, interpreters=interpreters,
                        pythonanywhere_domain=pa_domain)


@dataclasses.dataclass
class Options:
    target: Path
    repo_url: str = DEFAULT_REPO_URL
    branch: str = DEFAULT_BRANCH
    domain: str = ""
    venv: "Path | None" = None
    python: "str | None" = None
    wsgi_file: "Path | None" = None
    create_wsgi_file: bool = False
    pa_username: str = ""
    pa_host: str = "auto"
    use_api: bool = True
    api_token_file: "Path | None" = None
    github_token_file: "Path | None" = None
    assume_yes: bool = False
    interactive: "bool | None" = None
    dry_run: bool = False
    check_only: bool = False
    skip_deps: bool = False
    skip_wsgi: bool = False
    skip_webhook: bool = False
    skip_http: bool = False
    verify_push: bool = False
    rotate_secret: bool = False
    show_secret: bool = False
    update_webhook: bool = False
    adopt_wsgi: bool = False
    set_origin: bool = False
    switch_branch: bool = False
    restore_installer_file: bool = False
    recreate_venv: bool = False
    upgrade_pip: bool = False
    retries: int = 3
    timeout: int = 60
    pip_timeout: int = 900
    log_file: "Path | None" = None
    quiet: bool = False
    no_color: bool = False

    @property
    def venv_dir(self) -> Path:
        return Path(self.venv) if self.venv else self.target / DEFAULT_VENV_NAME


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=INSTALLER_NAME,
        description="Install, repair and verify the AMS deployment on PythonAnywhere.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Exit codes: 0 = complete, 2 = complete but manual steps remain, 1 = failed/blocked.",
    )
    parser.add_argument("--version", action="version", version="AMS setup_Deploy.py %s" % VERSION)
    parser.add_argument("--target", help="checkout directory (default: /home/rehmanahmed when it exists, else $HOME)")
    parser.add_argument("--repo", dest="repo_url", default=DEFAULT_REPO_URL, help="Git remote URL")
    parser.add_argument("--branch", default=DEFAULT_BRANCH, help="deployment branch (default: main)")
    parser.add_argument("--domain", help="PythonAnywhere site domain (default: rehmanahmed.pythonanywhere.com)")
    parser.add_argument("--venv", help="virtualenv directory (default: <target>/.venv)")
    parser.add_argument("--python", help="interpreter used to create the virtualenv, e.g. python3.10")
    parser.add_argument("--wsgi-file", dest="wsgi_file",
                        help="exact WSGI file path shown in the Web tab (never guessed when omitted)")
    parser.add_argument("--create-wsgi-file", action="store_true",
                        help="create the WSGI file at --wsgi-file if it does not exist yet")
    parser.add_argument("--pa-username", help="PythonAnywhere username (default: current user)")
    parser.add_argument("--pa-host", choices=("auto", "www", "eu"), default="auto",
                        help="PythonAnywhere API host (default: auto-detect)")
    parser.add_argument("--no-api", dest="use_api", action="store_false", help="never use the PythonAnywhere API")
    parser.add_argument("--api-token-file", help="file containing the PythonAnywhere API token (mode 0600)")
    parser.add_argument("--github-token-file", help="file containing a GitHub token (mode 0600)")
    parser.add_argument("-y", "--yes", dest="assume_yes", action="store_true",
                        help="answer yes to every confirmation (fully unattended)")
    parser.add_argument("--non-interactive", dest="interactive", action="store_false", default=None,
                        help="never prompt; material changes are reported instead of applied")
    parser.add_argument("--dry-run", action="store_true", help="show what would change; write nothing")
    parser.add_argument("--check-only", action="store_true",
                        help="run read-only detection and validation only; writes nothing at all "
                             "(add --log-file if you want a log anyway)")
    parser.add_argument("--skip-deps", action="store_true", help="do not install requirements.txt")
    parser.add_argument("--skip-wsgi", action="store_true", help="do not touch the PythonAnywhere WSGI file")
    parser.add_argument("--skip-webhook", action="store_true", help="do not touch the GitHub webhook")
    parser.add_argument("--skip-http", action="store_true", help="skip live HTTP checks against the site")
    parser.add_argument("--verify-push", action="store_true",
                        help="compare the server checkout with origin/<branch> after setup")
    parser.add_argument("--rotate-secret", action="store_true", help="rotate instance/deploy_secret.txt (asked first)")
    parser.add_argument("--show-secret", action="store_true",
                        help="print the webhook secret once, for copying into GitHub")
    parser.add_argument("--update-webhook", action="store_true",
                        help="allow updating an existing GitHub webhook (its secret cannot be read back)")
    parser.add_argument("--adopt-wsgi", action="store_true",
                        help="allow converting a hand-written AMS import in the WSGI file into the managed block")
    parser.add_argument("--set-origin", action="store_true", help="explicitly repoint a wrong 'origin' remote")
    parser.add_argument("--switch-branch", action="store_true", help="explicitly switch the checkout branch")
    parser.add_argument("--restore-installer-file", action="store_true",
                        help="archive a modified setup_Deploy.py and restore the tracked copy (explicit only)")
    parser.add_argument("--recreate-venv", action="store_true", help="rebuild a virtualenv with the wrong Python")
    parser.add_argument("--upgrade-pip", action="store_true", help="upgrade pip before installing requirements")
    parser.add_argument("--retries", type=int, default=3, help="retries for transient network errors (default: 3)")
    parser.add_argument("--timeout", type=int, default=60, help="per-command/HTTP timeout in seconds (default: 60)")
    parser.add_argument("--pip-timeout", type=int, default=900, help="pip install timeout per attempt (default: 900)")
    parser.add_argument("--log-file", help="setup log path (default: <target>/instance/setup/logs/...)")
    parser.add_argument("--quiet", action="store_true", help="only print warnings, results and the final summary")
    parser.add_argument("--no-color", action="store_true", help="disable ANSI colours")
    return parser


def parse_args(argv: "Sequence[str] | None" = None) -> Options:
    namespace = build_parser().parse_args(list(argv) if argv is not None else None)
    target_raw = namespace.target
    if target_raw:
        target = Path(target_raw).expanduser()
    elif Path(DEFAULT_TARGET).is_dir():
        target = Path(DEFAULT_TARGET)
    else:
        target = Path.home()
    target = Path(os.path.abspath(str(target)))
    domain = namespace.domain or (DEFAULT_DOMAIN if str(target) == DEFAULT_TARGET
                                  else "%s.pythonanywhere.com" % (os.environ.get("USER") or "user"))
    return Options(
        target=target,
        repo_url=namespace.repo_url,
        branch=namespace.branch,
        domain=domain,
        venv=Path(namespace.venv).expanduser() if namespace.venv else None,
        python=namespace.python,
        wsgi_file=Path(namespace.wsgi_file).expanduser() if namespace.wsgi_file else None,
        create_wsgi_file=namespace.create_wsgi_file,
        pa_username=namespace.pa_username or "",
        pa_host=namespace.pa_host,
        use_api=namespace.use_api,
        api_token_file=Path(namespace.api_token_file).expanduser() if namespace.api_token_file else None,
        github_token_file=Path(namespace.github_token_file).expanduser() if namespace.github_token_file else None,
        assume_yes=namespace.assume_yes,
        interactive=namespace.interactive,
        dry_run=namespace.dry_run,
        check_only=namespace.check_only,
        skip_deps=namespace.skip_deps,
        skip_wsgi=namespace.skip_wsgi,
        skip_webhook=namespace.skip_webhook,
        skip_http=namespace.skip_http,
        verify_push=namespace.verify_push,
        rotate_secret=namespace.rotate_secret,
        show_secret=namespace.show_secret,
        update_webhook=namespace.update_webhook,
        adopt_wsgi=namespace.adopt_wsgi,
        set_origin=namespace.set_origin,
        switch_branch=namespace.switch_branch,
        restore_installer_file=namespace.restore_installer_file,
        recreate_venv=namespace.recreate_venv,
        upgrade_pip=namespace.upgrade_pip,
        retries=max(0, int(namespace.retries)),
        timeout=max(5, int(namespace.timeout)),
        pip_timeout=max(30, int(namespace.pip_timeout)),
        log_file=Path(namespace.log_file).expanduser() if namespace.log_file else None,
        quiet=namespace.quiet,
        no_color=namespace.no_color,
    )


# --------------------------------------------------------------------------- #
# Output: redacted console output + redacted setup log + staged check records
# --------------------------------------------------------------------------- #
_STATUS_COLOR = {PASS: "32", FAIL: "31", MANUAL: "33", WARN: "33", SKIP: "2", INFO: "36"}
_STATUS_LABEL = {
    PASS: "ok",
    FAIL: "FAIL",
    MANUAL: "todo",
    WARN: "warn",
    SKIP: "skip",
    INFO: "info",
}


class Reporter:
    """Single place that prints, logs and records check results."""

    def __init__(self, *, stream: Any = None, err_stream: Any = None, log_path: "Path | None" = None,
                 color: bool = True, quiet: bool = False, redactor: "Redactor | None" = None) -> None:
        self.stream = stream if stream is not None else sys.stdout
        self.err_stream = err_stream if err_stream is not None else sys.stderr
        self.quiet = quiet
        self.color = bool(color and getattr(self.stream, "isatty", lambda: False)())
        self.redactor = redactor or Redactor()
        self.checks: "list[Check]" = []
        self.current_stage = "preflight"
        self._log_lines: "list[str]" = []
        self.log_path = log_path
        if log_path is not None:
            try:
                ensure_dir(Path(log_path).parent, 0o700)
                if not Path(log_path).exists():
                    atomic_write(Path(log_path), "# AMS setup_Deploy.py %s\n" % VERSION, 0o600)
                else:
                    os.chmod(str(log_path), 0o600)
            except OSError:
                self.log_path = None

    # -- primitives ------------------------------------------------------- #
    def _colorize(self, text: str, code: str) -> str:
        return "\x1b[%sm%s\x1b[0m" % (code, text) if self.color else text

    def _write(self, text: str, stream: Any = None) -> None:
        stream = stream if stream is not None else self.stream
        try:
            stream.write(text + "\n")
            stream.flush()
        except OSError:  # pragma: no cover - closed pipe
            pass

    def raw(self, text: str = "", *, log: bool = False, stream: Any = None) -> None:
        """Print without redaction (used only for explicitly requested secret display)."""
        self._write(text, stream=stream)
        if log:
            self.log(text)

    def log(self, text: str) -> None:
        line = self.redactor.redact(text)
        self._log_lines.append(line)
        if self.log_path is not None:
            try:
                with open(str(self.log_path), "a", encoding="utf-8") as handle:
                    handle.write("%s %s\n" % (time.strftime("%H:%M:%S"), line))
            except OSError:
                pass

    def _emit(self, text: str, *, stream: Any = None, quiet_ok: bool = False) -> None:
        safe = self.redactor.redact(text)
        self._write(safe, stream=stream)
        self.log(safe.rstrip())

    # -- message helpers --------------------------------------------------- #
    def info(self, text: str) -> None:
        if not self.quiet:
            self._emit(text)

    def note(self, text: str) -> None:
        self._emit(text)

    def ok(self, text: str) -> None:
        self._emit("  %s %s" % (self._colorize("[ok]", "32"), text))

    def warn(self, text: str) -> None:
        self._emit("  %s %s" % (self._colorize("[warn]", "33"), text))

    def error(self, text: str) -> None:
        self._emit("  %s %s" % (self._colorize("[error]", "31"), text))

    def todo(self, text: str) -> None:
        self._emit("  %s %s" % (self._colorize("[todo]", "33"), text))

    def detail(self, text: str) -> None:
        if self.quiet:
            return
        for line in str(text).splitlines():
            self._emit("       %s" % line)

    def section(self, title: str) -> None:
        self._emit("")
        self._emit(self._colorize(title, "1"))

    def stage(self, key: str, title: str) -> None:
        self.current_stage = key
        self._emit("")
        self._emit(self._colorize("[%s] %s" % (key, title), "1"))
        self.log("STAGE %s: %s" % (key, title))

    def banner(self, text: str, code: str = "1") -> None:
        rule = "-" * min(72, max(24, len(text) + 6))
        self._emit(rule)
        self._emit(self._colorize(text, code))
        self._emit(rule)

    # -- check records ----------------------------------------------------- #
    def check(self, name: str, status: str, detail: str = "", fix: str = "",
              required: bool = True, stage: "str | None" = None) -> Check:
        record = Check(stage=stage or self.current_stage, name=name, status=status,
                       detail=detail, fix=fix, required=required)
        self.checks.append(record)
        marker = {
            PASS: self._colorize("[ok]", "32"),
            FAIL: self._colorize("[FAIL]", "31"),
            MANUAL: self._colorize("[todo]", "33"),
            WARN: self._colorize("[warn]", "33"),
            SKIP: self._colorize("[skip]", "2"),
            INFO: self._colorize("[info]", "36"),
        }[status]
        line = "  %s %s" % (marker, name)
        if detail:
            line += " — %s" % detail
        self._emit(line)
        if fix and status in (FAIL, MANUAL):
            self._emit("       next: %s" % fix)
        return record

    def counts(self) -> "dict[str, int]":
        counts = {PASS: 0, FAIL: 0, MANUAL: 0, WARN: 0, SKIP: 0, INFO: 0}
        for check in self.checks:
            counts[check.status] = counts.get(check.status, 0) + 1
        return counts

    def blockers(self) -> "list[Check]":
        return [c for c in self.checks if c.status == FAIL and c.required]

    def manual(self) -> "list[Check]":
        return [c for c in self.checks if c.status == MANUAL]

    def exit_code(self) -> int:
        if self.blockers():
            return 1
        if self.manual():
            return 2
        return 0

    def log_text(self) -> str:
        return "\n".join(self._log_lines)


class Prompter:
    """Interactive confirmations with explicit non-interactive semantics.

    ``--yes`` answers yes to everything (recorded in the log).  Without a TTY
    and without ``--yes`` the answer is *no* so material changes are never
    applied unattended; the caller reports them as manual steps instead.
    """

    def __init__(self, *, assume_yes: bool = False, interactive: "bool | None" = None,
                 reporter: "Reporter | None" = None, input_fn: Any = None, out_fn: Any = None) -> None:
        self.assume_yes = assume_yes
        if interactive is None:
            interactive = bool(getattr(sys.stdin, "isatty", lambda: False)()) and not assume_yes
        self.interactive = bool(interactive)
        self.reporter = reporter
        self._input = input_fn
        self._out = out_fn

    def _say(self, text: str) -> None:
        if self.reporter is not None:
            self.reporter.note(text)
        else:
            print(text)

    def _readline(self, prompt: str) -> str:
        if self._input is not None:
            return self._input(prompt)
        return input(prompt)

    def confirm(self, question: str, *, default: bool = True, material: bool = False,
                detail: "str | None" = None, noninteractive_default: "bool | None" = None) -> bool:
        if self.assume_yes:
            self._say("  %s %s [auto-yes]" % ("!" if material else "*", question))
            return True
        if not self.interactive:
            if noninteractive_default is not None:
                self._say("  * %s -> %s (non-interactive run)" % (question,
                                                                  "proceeding" if noninteractive_default else "skipped"))
                return noninteractive_default
            self._say("  %s %s -> not applied (no confirmation available in this mode)" % ("!" if material else "*", question))
            return False
        if detail:
            for line in str(detail).splitlines():
                print("      " + line)
        suffix = " [Y/n]" if default else " [y/N]"
        try:
            answer = self._readline("  ? %s%s " % (question, suffix)).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("")
            return False
        if not answer:
            return default
        return answer in ("y", "yes")

    def ask(self, question: str, default: str = "") -> str:
        if self.assume_yes or not self.interactive:
            return default
        suffix = " [%s]" % default if default else ""
        try:
            answer = self._readline("  ? %s%s " % (question, suffix)).strip()
        except (EOFError, KeyboardInterrupt):
            print("")
            return default
        return answer or default

    def hidden(self, prompt: str) -> str:
        if self.assume_yes or not self.interactive:
            return ""
        try:
            return getpass.getpass(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print("")
            return ""

    def skipped(self, what: str) -> None:
        self._say("  * %s -> skipped (needs confirmation; rerun with --yes or in a Bash console)" % what)


# --------------------------------------------------------------------------- #
# External commands
# --------------------------------------------------------------------------- #
class CommandRunner:
    """Runs external commands with timeouts, redaction and dry-run support."""

    def __init__(self, *, reporter: "Reporter", dry_run: bool = False,
                 base_env: "dict[str, str] | None" = None, default_timeout: int = 120) -> None:
        self.reporter = reporter
        self.dry_run = dry_run
        self.base_env = dict(os.environ if base_env is None else base_env)
        self.default_timeout = default_timeout
        self.commands: "list[list[str]]" = []

    def _env(self, extra: "dict[str, str] | None") -> "dict[str, str]":
        env = dict(self.base_env)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        if extra:
            env.update(extra)
        return env

    def run(self, argv: "Sequence[str]", *, cwd: "Path | None" = None, timeout: "int | None" = None,
            input_text: "str | None" = None, extra_env: "dict[str, str] | None" = None,
            mutating: bool = False, label: "str | None" = None) -> Result:
        argv = [str(part) for part in argv]
        self.commands.append(list(argv))
        shown = " ".join(argv)
        if label:
            shown = "%s (%s)" % (shown, label)
        if mutating and self.dry_run:
            self.reporter.info("  [dry-run] would run: %s" % shown)
            return Result(argv=argv, rc=0, out="", skipped=True)
        self.reporter.log("$ %s" % shown)
        try:
            proc = subprocess.run(argv, cwd=str(cwd) if cwd else None,
                                  capture_output=True, text=True, timeout=timeout or self.default_timeout,
                                  input=input_text, env=self._env(extra_env))
        except subprocess.TimeoutExpired as exc:
            out = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            result = Result(argv=argv, rc=124, out=out, err="command timed out after %ss" % (timeout or self.default_timeout),
                            timed_out=True)
        except FileNotFoundError as exc:
            result = Result(argv=argv, rc=127, err="command not found: %s" % exc)
        except OSError as exc:
            result = Result(argv=argv, rc=126, err=str(exc))
        else:
            result = Result(argv=argv, rc=proc.returncode, out=proc.stdout or "", err=proc.stderr or "")
        if result.out:
            self.reporter.log(result.out.rstrip())
        if result.err:
            self.reporter.log(result.err.rstrip())
        return result

    def git(self, *args: str, cwd: Path, mutating: bool = False, timeout: "int | None" = None,
            label: "str | None" = None) -> Result:
        env = {
            "GIT_TERMINAL_PROMPT": "0",     # never block on a credential prompt
            "GCM_INTERACTIVE": "never",
            "GIT_ADVICE": "0",
            "LC_ALL": "C",
            "LANG": "C",
        }
        return self.run(["git", *args], cwd=cwd, timeout=timeout or self.default_timeout,
                        extra_env=env, mutating=mutating, label=label)


# --------------------------------------------------------------------------- #
# HTTP (TLS verification is never disabled)
# --------------------------------------------------------------------------- #
class HttpClient:
    def __init__(self, *, reporter: "Reporter", dry_run: bool = False, timeout: int = 30,
                 retries: int = 2, backoff: "Sequence[float]" = (1.0, 3.0, 7.0)) -> None:
        self.reporter = reporter
        self.dry_run = dry_run
        self.timeout = timeout
        self.retries = max(0, retries)
        self.backoff = list(backoff)
        self.user_agent = "AMS-setup_Deploy/%s" % VERSION
        self._context = ssl.create_default_context()

    def _sleep(self, attempt: int) -> None:
        delay = self.backoff[min(attempt, len(self.backoff) - 1)]
        time.sleep(delay)

    def request(self, method: str, url: str, *, data: "bytes | str | None" = None,
                headers: "dict[str, str] | None" = None, json_body: Any = None,
                timeout: "int | None" = None, retries: "int | None" = None,
                mutating: "bool | None" = None, label: "str | None" = None) -> HttpResponse:
        method = method.upper()
        if mutating is None:
            mutating = method not in ("GET", "HEAD")
        if json_body is not None and data is None:
            data = json.dumps(json_body).encode("utf-8")
        if isinstance(data, bytearray):
            data = bytes(data)
        if isinstance(data, str):
            data = data.encode("utf-8")
        safe_url = redact_url(url)
        if mutating and self.dry_run:
            self.reporter.info("  [dry-run] would %s %s" % (method, safe_url))
            return HttpResponse(url=safe_url, skipped=True, status=0)
        attempts = (self.retries if retries is None else max(0, retries)) + 1
        last = HttpResponse(url=safe_url)
        for attempt in range(attempts):
            request = urllib.request.Request(url, data=data, method=method)
            request.add_header("User-Agent", self.user_agent)
            request.add_header("Accept", "*/*")
            for key, value in (headers or {}).items():
                request.add_header(key, value)
            started = time.time()
            try:
                with urllib.request.urlopen(request, timeout=timeout or self.timeout, context=self._context) as response:
                    body = response.read().decode("utf-8", "replace")
                    self.reporter.log("HTTP %s %s -> %s (%.1fs)" % (method, safe_url, response.status, time.time() - started))
                    return HttpResponse(status=int(response.status), body=body,
                                        headers={k.lower(): v for k, v in response.headers.items()},
                                        url=safe_url)
            except urllib.error.HTTPError as exc:
                body = ""
                try:
                    body = exc.read().decode("utf-8", "replace")
                except Exception:  # pragma: no cover - unreadable error body
                    body = ""
                status = int(getattr(exc, "code", 0) or 0)
                self.reporter.log("HTTP %s %s -> %s (%.1fs)" % (method, safe_url, status, time.time() - started))
                last = HttpResponse(status=status, body=body, url=safe_url,
                                    headers={k.lower(): v for k, v in (exc.headers or {}).items()})
                if status < 500 and status != 429:
                    return last
            except (urllib.error.URLError, socket.timeout, TimeoutError, ssl.SSLError, ConnectionError, OSError) as exc:
                reason = getattr(exc, "reason", None) or exc
                last = HttpResponse(status=0, url=safe_url, error=str(reason))
                self.reporter.log("HTTP %s %s -> error: %s" % (method, safe_url, reason))
                if isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(reason):
                    last.error = "TLS verification failed: %s" % reason
                    return last
            if attempt + 1 < attempts:
                self.reporter.log("retrying %s %s (attempt %d/%d)" % (method, safe_url, attempt + 2, attempts))
                self._sleep(attempt)
        if label:
            self.reporter.log("%s failed after %d attempt(s)" % (label, attempts))
        return last


# --------------------------------------------------------------------------- #
# PythonAnywhere API (documented endpoints only)
# --------------------------------------------------------------------------- #
class PythonAnywhereAPI:
    """Documented PythonAnywhere API calls used for Web-tab automation.

    Endpoints (help.pythonanywhere.com/pages/API):
      GET    /api/v0/user/{u}/webapps/
      POST   /api/v0/user/{u}/webapps/                     (domain_name, python_version)
      GET    /api/v0/user/{u}/webapps/{domain}/
      PATCH  /api/v0/user/{u}/webapps/{domain}/            (source_directory, virtualenv_path, ...)
      GET    /api/v0/user/{u}/webapps/{domain}/static_files/
      POST   /api/v0/user/{u}/webapps/{domain}/static_files/  (url, path)
      PATCH  /api/v0/user/{u}/webapps/{domain}/static_files/{id}/
      POST   /api/v0/user/{u}/webapps/{domain}/reload/
    """

    def __init__(self, *, username: str, token: str, host: str, http: HttpClient,
                 reporter: "Reporter", domain: str) -> None:
        self.username = username
        self.token = token
        self.host = "www" if host == "auto" else host
        self.host_requested = host
        self.http = http
        self.reporter = reporter
        self.domain = domain

    @property
    def base(self) -> str:
        return "https://%s.pythonanywhere.com" % self.host

    def _headers(self) -> "dict[str, str]":
        return {"Authorization": "Token %s" % self.token, "Accept": "application/json"}

    def api(self, method: str, path: str, *, json_body: Any = None, retries: "int | None" = None,
            label: "str | None" = None, mutating: "bool | None" = None) -> HttpResponse:
        return self.http.request(method, self.base + path, headers=self._headers(),
                                 json_body=json_body, retries=retries, label=label or path,
                                 mutating=mutating)

    def resolve_host(self) -> bool:
        hosts = ["www", "eu"] if self.host_requested == "auto" else [self.host_requested]
        for host in hosts:
            self.host = host
            response = self.api("GET", "/api/v0/user/%s/cpu/" % self.username, retries=0, label="token check")
            if response.ok:
                return True
            if response.status in (401, 403):
                return False
        return False

    def list_webapps(self) -> "HttpResponse":
        return self.api("GET", "/api/v0/user/%s/webapps/" % self.username, label="list web apps")

    def get_webapp(self) -> "HttpResponse":
        return self.api("GET", "/api/v0/user/%s/webapps/%s/" % (self.username, self.domain),
                        retries=0, label="get web app config")

    def create_webapp(self, python_version: str) -> "HttpResponse":
        return self.api("POST", "/api/v0/user/%s/webapps/" % self.username,
                        json_body={"domain_name": self.domain, "python_version": python_version},
                        retries=0, label="create web app")

    def patch_webapp(self, payload: "dict[str, Any]") -> "HttpResponse":
        return self.api("PATCH", "/api/v0/user/%s/webapps/%s/" % (self.username, self.domain),
                        json_body=payload, retries=1, label="update web app config")

    def static_mappings(self) -> "HttpResponse":
        return self.api("GET", "/api/v0/user/%s/webapps/%s/static_files/" % (self.username, self.domain),
                        retries=1, label="list static mappings")

    def create_static(self, url: str, path: str) -> "HttpResponse":
        return self.api("POST", "/api/v0/user/%s/webapps/%s/static_files/" % (self.username, self.domain),
                        json_body={"url": url, "path": path}, retries=1, label="create static mapping")

    def update_static(self, mapping_id: Any, url: str, path: str) -> "HttpResponse":
        return self.api("PATCH", "/api/v0/user/%s/webapps/%s/static_files/%s/" % (self.username, self.domain, mapping_id),
                        json_body={"url": url, "path": path}, retries=1, label="update static mapping")

    def reload_webapp(self) -> "HttpResponse":
        return self.api("POST", "/api/v0/user/%s/webapps/%s/reload/" % (self.username, self.domain),
                        json_body={}, retries=0, label="reload web app")


# --------------------------------------------------------------------------- #
# GitHub API (documented endpoints only)
# --------------------------------------------------------------------------- #
class GitHubAPI:
    def __init__(self, *, slug: str, token: str, http: HttpClient, reporter: "Reporter",
                 api_base: str = "https://api.github.com") -> None:
        self.slug = slug
        self.token = token
        self.http = http
        self.reporter = reporter
        self.api_base = api_base.rstrip("/")

    def _headers(self) -> "dict[str, str]":
        return {
            "Authorization": "Bearer %s" % self.token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def api(self, method: str, path: str, *, json_body: Any = None, retries: "int | None" = None,
            label: "str | None" = None) -> HttpResponse:
        return self.http.request(method, self.api_base + path, headers=self._headers(),
                                 json_body=json_body, retries=retries,
                                 label=label or "GitHub %s" % path)

    def list_hooks(self, pages: int = 3) -> "tuple[list[dict], HttpResponse]":
        hooks: "list[dict]" = []
        last = HttpResponse()
        for page in range(1, pages + 1):
            last = self.api("GET", "/repos/%s/hooks?per_page=100&page=%d" % (self.slug, page),
                            retries=1, label="list webhooks (page %d)" % page)
            if not last.ok:
                return hooks, last
            try:
                batch = last.json()
            except ValueError:
                return hooks, HttpResponse(status=last.status, body=last.body, error="invalid JSON from GitHub")
            if not isinstance(batch, list):
                return hooks, HttpResponse(status=last.status, body=last.body, error="unexpected webhook payload")
            hooks.extend(item for item in batch if isinstance(item, dict))
            if len(batch) < 100:
                break
        return hooks, last

    def create_hook(self, payload: "dict[str, Any]") -> "HttpResponse":
        return self.api("POST", "/repos/%s/hooks" % self.slug, json_body=payload, retries=0, label="create webhook")

    def update_hook(self, hook_id: Any, payload: "dict[str, Any]") -> "HttpResponse":
        return self.api("PATCH", "/repos/%s/hooks/%s" % (self.slug, hook_id), json_body=payload,
                        retries=0, label="update webhook")

    def get_hook(self, hook_id: Any) -> "HttpResponse":
        return self.api("GET", "/repos/%s/hooks/%s" % (self.slug, hook_id), retries=1, label="read webhook")

    def ping_hook(self, hook_id: Any) -> "HttpResponse":
        return self.api("POST", "/repos/%s/hooks/%s/pings" % (self.slug, hook_id), retries=0, label="send ping")

    def list_deliveries(self, hook_id: Any) -> "tuple[list[dict], HttpResponse]":
        """Recent deliveries of one webhook (what GitHub shows under Recent Deliveries)."""
        response = self.api("GET", "/repos/%s/hooks/%s/deliveries?per_page=30" % (self.slug, hook_id),
                            retries=1, label="read webhook deliveries")
        if not response.ok:
            return [], response
        try:
            payload = response.json()
        except ValueError:
            return [], HttpResponse(status=response.status, body=response.body, error="invalid JSON from GitHub")
        if not isinstance(payload, list):
            return [], HttpResponse(status=response.status, body=response.body, error="unexpected deliveries payload")
        return [item for item in payload if isinstance(item, dict)], response


def hook_payload(url: str, secret: str) -> "dict[str, Any]":
    return {
        "name": "web",
        "active": True,
        "events": ["push"],
        "config": {
            "url": url,
            "content_type": "application/json",
            "secret": secret,
            "insecure_ssl": "0",
        },
    }


# --------------------------------------------------------------------------- #
# Detected state (read-only) and the installer itself
# --------------------------------------------------------------------------- #
@dataclasses.dataclass
class Detected:
    is_git_repo: bool = False
    origin_url: str = ""
    branch: str = ""
    head: str = ""
    dirty: "list[str]" = dataclasses.field(default_factory=list)
    tracked_dirty: "list[str]" = dataclasses.field(default_factory=list)
    missing_paths: "list[str]" = dataclasses.field(default_factory=list)
    hook_present: bool = False
    hook_missing_markers: "list[str]" = dataclasses.field(default_factory=list)
    hook_missing_recommended: "list[str]" = dataclasses.field(default_factory=list)
    hook_configured_in_wsgi: bool = False
    venv_present: bool = False
    venv_python: str = ""
    venv_version: str = ""
    secret_present: bool = False
    secret_valid: bool = False
    secret_fingerprint: str = ""
    secret_mode: int = 0
    stale_root_secret: bool = False
    wsgi_file: str = ""
    wsgi_source: str = ""
    wsgi_exists: bool = False
    wsgi_has_managed: bool = False
    wsgi_has_unmanaged_ams: bool = False
    wsgi_foreign: bool = False
    db_files: "list[str]" = dataclasses.field(default_factory=list)
    custom_db_path: str = ""
    state: "dict[str, Any]" = dataclasses.field(default_factory=dict)
    state_version: str = ""


class Installer:
    """Ordered stages, all of them re-runnable and none of them destructive."""

    def __init__(self, options: Options, *, runner: "CommandRunner | None" = None,
                 http: "HttpClient | None" = None, prompt: "Prompter | None" = None,
                 reporter: "Reporter | None" = None, env: "dict[str, str] | None" = None,
                 platform_info: "PlatformInfo | None" = None,
                 clock: Any = time.time, sleep: Any = time.sleep) -> None:
        self.o = options
        self.env = dict(os.environ if env is None else env)
        self.platform = platform_info or detect_platform(self.env)
        log_path = options.log_file
        if log_path is None and not (options.dry_run or options.check_only):
            log_path = options.target / "instance" / "setup" / "logs" / ("setup-%s.log" % now_stamp())
        self.reporter = reporter or Reporter(
            log_path=log_path,
            color=not options.no_color,
            quiet=options.quiet,
        )
        self.redactor = self.reporter.redactor
        self.runner = runner or CommandRunner(reporter=self.reporter, dry_run=options.dry_run,
                                              base_env=self.env, default_timeout=options.timeout)
        self.http = http or HttpClient(reporter=self.reporter, dry_run=options.dry_run,
                                       timeout=options.timeout, retries=min(2, options.retries))
        self.prompt = prompt or Prompter(assume_yes=options.assume_yes, interactive=options.interactive,
                                         reporter=self.reporter)
        self.clock = clock
        self.sleep = sleep
        self.detected = Detected()
        self.record: "dict[str, Any]" = {}
        self.secret_value = ""
        self.api: "PythonAnywhereAPI | None" = None
        self.web_app: "dict[str, Any] | None" = None
        self.web_app_python_version = ""
        self.payload_url = ""
        self.github_token = ""
        self._lock_handle = None
        self._lock_path: "Path | None" = None
        self._deps_validated = False
        self._web_app_ready = False
        self.var_www = Path(getattr(self.platform, "var_www", Path("/var/www")))
        self.installer_path = Path(os.path.abspath(__file__))

    # -- paths ------------------------------------------------------------- #
    @property
    def target(self) -> Path:
        return self.o.target

    @property
    def runtime_dir(self) -> Path:
        return self.target / "instance" / "setup"

    @property
    def logs_dir(self) -> Path:
        return self.runtime_dir / "logs"

    @property
    def state_path(self) -> Path:
        return self.runtime_dir / "state.json"

    @property
    def secret_path(self) -> Path:
        override = self.env.get("AMS_DEPLOY_SECRET_FILE")
        if override:
            return Path(override).expanduser()
        return self.target / "instance" / "deploy_secret.txt"

    @property
    def venv_dir(self) -> Path:
        return self.o.venv_dir

    @property
    def venv_python(self) -> Path:
        return self.venv_dir / "bin" / "python"

    @property
    def hook_path(self) -> Path:
        return self.target / HOOK_FILE

    @property
    def wsgi_entry(self) -> Path:
        return self.target / WSGI_ENTRY

    @property
    def requirements(self) -> Path:
        return self.target / "requirements.txt"

    def may_write(self) -> bool:
        return not self.o.dry_run and not self.o.check_only

    def _p(self, *parts: str) -> str:
        return str(self.target.joinpath(*parts))

    # -- backups ----------------------------------------------------------- #
    def backup_file(self, source: Path, label: str) -> "Path | None":
        """Copy a file into the private backup tree before it is changed."""
        if not self.may_write():
            self.reporter.info("  [%s] would back up %s (%s)" %
                               ("dry-run" if self.o.dry_run else "check-only", source, label))
            return None
        stamp = now_stamp()
        destination_dir = self.runtime_dir / "backups" / stamp
        ensure_dir(destination_dir, 0o700)
        destination = destination_dir / ("%s--%s" % (label, source.name))
        counter = 1
        while destination.exists():
            destination = destination_dir / ("%s--%s.%d" % (label, source.name, counter))
            counter += 1
        shutil.copy2(str(source), str(destination))
        os.chmod(str(destination), 0o600)
        manifest = destination_dir / "manifest.json"
        entries: "list[dict[str, Any]]" = []
        if manifest.exists():
            try:
                entries = json.loads(read_text(manifest))
            except ValueError:
                entries = []
        entries.append({"source": str(source), "backup": str(destination), "label": label,
                        "sha256": sha256_file(destination), "at": now_iso()})
        atomic_write(manifest, json.dumps(entries, indent=2), 0o600)
        self.reporter.ok("backed up %s -> %s" % (source, destination))
        return destination

    def load_state(self) -> "dict[str, Any]":
        if self.state_path.exists():
            try:
                data = json.loads(read_text(self.state_path))
                if isinstance(data, dict):
                    return data
            except ValueError:
                pass
        return {}

    def save_state(self) -> None:
        if not self.may_write():
            return
        ensure_dir(self.runtime_dir, 0o700)
        payload = dict(self.record)
        payload.update({
            "installer_version": VERSION,
            "updated_at": now_iso(),
            "target": str(self.target),
            "branch": self.o.branch,
            "domain": self.o.domain,
        })
        atomic_write(self.state_path, json.dumps(payload, indent=2, sort_keys=True), 0o600)

    # -- detection --------------------------------------------------------- #
    def detect(self) -> Detected:
        detected = Detected()
        target = self.target
        if target.is_dir():
            probe = self.runner.git("rev-parse", "--git-dir", cwd=target, timeout=20)
            detected.is_git_repo = probe.ok
            if detected.is_git_repo:
                origin = self.runner.git("remote", "get-url", "origin", cwd=target, timeout=20)
                detected.origin_url = origin.out.strip() if origin.ok else ""
                branch = self.runner.git("branch", "--show-current", cwd=target, timeout=20)
                detected.branch = branch.out.strip() if branch.ok else ""
                head = self.runner.git("rev-parse", "--short", "HEAD", cwd=target, timeout=20)
                detected.head = head.out.strip() if head.ok else ""
                status = self.runner.git("status", "--porcelain", cwd=target, timeout=30)
                if status.ok:
                    for line in status.out.splitlines():
                        line = line.rstrip()
                        if not line:
                            continue
                        detected.dirty.append(line)
                        if not line.startswith("??"):
                            detected.tracked_dirty.append(line[3:] if len(line) > 3 else line)
            detected.missing_paths = [rel for rel in REQUIRED_PATHS if not (target / rel).exists()]
        detected.state = self.load_state()
        detected.state_version = str(detected.state.get("installer_version") or "")

        hook = self.hook_path
        if hook.is_file():
            detected.hook_present = True
            text = read_text(hook)
            detected.hook_missing_markers = [m for m in HOOK_REQUIRED_MARKERS if m not in text]
            detected.hook_missing_recommended = [m for m in HOOK_RECOMMENDED_MARKERS if m not in text]
        if self.wsgi_entry.is_file():
            text = read_text(self.wsgi_entry)
            detected.hook_configured_in_wsgi = all(marker in text for marker in HOOK_MOUNT_MARKERS)

        if self.venv_python.is_file():
            detected.venv_present = True
            detected.venv_python = str(self.venv_python)
            proc = self.runner.run([str(self.venv_python), "-c", "import sys;print('%d.%d.%d' % sys.version_info[:3])"],
                                   timeout=30)
            detected.venv_version = proc.out.strip() if proc.ok else ""

        secret = self.secret_path
        if secret.is_file():
            detected.secret_present = True
            detected.secret_mode = file_mode(secret)
            try:
                value = read_text(secret).strip()
            except OSError:
                value = ""
            detected.secret_valid = is_secret_value_valid(value)
            if value:
                detected.secret_fingerprint = fingerprint(value)
        detected.stale_root_secret = (target / EXPOSED_SECRET_REL).is_file()

        instance = target / "instance"
        if instance.is_dir():
            detected.db_files = sorted(str(p.relative_to(target)) for p in instance.glob("*.db"))
        detected.wsgi_file, detected.wsgi_source = self.resolve_wsgi_path(detected)
        if detected.wsgi_file:
            path = Path(detected.wsgi_file)
            detected.wsgi_exists = path.is_file()
            if detected.wsgi_exists:
                text = normalize_wsgi_source(read_text(path))
                detected.wsgi_has_managed = MANAGED_BEGIN in text
                detected.wsgi_has_unmanaged_ams = any(marker in text for marker in WSGI_WRAP_MARKERS) \
                    and not detected.wsgi_has_managed
                detected.wsgi_foreign = self._looks_like_another_app(text)
                for line in text.splitlines():
                    if "APP_DB_PATH" in line and "=" in line:
                        detected.custom_db_path = line.strip()
                        break
        self.detected = detected
        return detected

    def _looks_like_another_app(self, text: str) -> bool:
        """True when the WSGI file looks like a different application's config."""
        own = any(token in text for token in (str(self.target), "deploy_hook", "AMS_WSGI_FILE", MANAGED_BEGIN))
        foreign = bool(re.search(r"^\s*(from|import)\s+\w*(django|flask|bottle|py4web)\w*", text, re.MULTILINE))
        foreign = foreign or bool(re.search(r"get_wsgi_application\(\)", text))
        return foreign and not own

    def resolve_wsgi_path(self, detected: "Detected | None" = None) -> "tuple[str, str]":
        """Return (path, source); never invents a path that does not exist.

        Order: --wsgi-file, then AMS_WSGI_FILE, then the domain-derived
        /var/www/<domain>_wsgi.py that PythonAnywhere shows in the Web tab.
        """
        if self.o.wsgi_file:
            return str(self.o.wsgi_file), "option"
        override = self.env.get("AMS_WSGI_FILE")
        if override and not override.strip().startswith("warning"):
            return str(Path(override).expanduser()), "env"
        derived = derive_wsgi_path(self.var_www, self.o.domain)
        if derived.is_file():
            return str(derived), "derived"
        return str(derived), "derived-missing"

    # -- plan -------------------------------------------------------------- #
    def build_plan(self) -> "list[str]":
        detected = self.detected
        plan: "list[str]" = []
        plan.append("Target: %s%s" % (self.target,
                                      "  (home-directory checkout — no AMS subfolder)" if self._is_home() else ""))
        plan.append("Repository: %s  (branch %s)" % (self.o.repo_url, self.o.branch))
        plan.append("Site domain: %s  (WSGI file: %s [%s])" % (self.o.domain, detected.wsgi_file, detected.wsgi_source))
        if not detected.is_git_repo:
            plan.append("Git: bootstrap a new checkout in place (git init + fetch + checkout; untracked home files kept)")
        elif normalize_repo_url(detected.origin_url) != normalize_repo_url(self.o.repo_url):
            plan.append("Git: STOP — origin is %s, expected %s (use --set-origin to repoint deliberately)"
                        % (detected.origin_url or "<none>", self.o.repo_url))
        else:
            plan.append("Git: fast-forward only update of %s (local modifications are never discarded)" % self.o.branch)
        if self.o.skip_deps:
            plan.append("Python: skipped (--skip-deps)")
        elif detected.venv_present and detected.venv_version:
            plan.append("Python: reuse %s (Python %s)" % (self.venv_dir, detected.venv_version))
        else:
            plan.append("Python: create %s (Web tab Python version must match the virtualenv)" % self.venv_dir)
        if self.o.skip_deps:
            plan.append("Dependencies: skipped (--skip-deps)")
        else:
            plan.append("Dependencies: pip install -r requirements.txt into the virtualenv (timeout %ss)" % self.o.pip_timeout)
        if detected.secret_present and detected.secret_valid:
            plan.append("Secret: keep the existing %s (%s)" % (self.secret_path, detected.secret_fingerprint))
        else:
            plan.append("Secret: create %s with secrets.token_hex(32), mode 0600" % self.secret_path)
        if self.o.skip_wsgi:
            plan.append("WSGI: skipped (--skip-wsgi)")
        elif detected.wsgi_exists:
            action = "replace the managed AMS block in" if detected.wsgi_has_managed else "add one managed AMS block to"
            plan.append("WSGI: %s %s (backup first)" % (action, detected.wsgi_file))
        elif self.o.create_wsgi_file:
            plan.append("WSGI: create %s (only because --create-wsgi-file was given)" % detected.wsgi_file)
        else:
            plan.append("WSGI: NOT touched — %s does not exist; the exact file is printed for the Web tab"
                        % detected.wsgi_file)
        plan.append("PythonAnywhere API: %s" % ("use a token if one is available (source dir, virtualenv, static mapping, reload)"
                                                if self.o.use_api else "disabled (--no-api)"))
        plan.append("GitHub webhook: %s" % ("create/verify with a token if available, else print the exact manual steps"
                                            if not self.o.skip_webhook else "skipped (--skip-webhook)"))
        plan.append("Validation: files, venv, WSGI syntax, HTTP reachability%s, signed ping, webhook, push/pull"
                    % ("" if self.o.skip_http else ", /deploy/health"))
        return plan

    def _is_home(self) -> bool:
        try:
            return self.target.resolve() == Path(self.env.get("HOME", str(Path.home()))).expanduser().resolve()
        except OSError:  # pragma: no cover - defensive
            return False

    def print_plan(self) -> None:
        self.reporter.section("Plan")
        for index, line in enumerate(self.build_plan(), start=1):
            self.reporter.note("  %d. %s" % (index, line))
        if self.o.dry_run:
            self.reporter.note("")
            self.reporter.note("  dry run: nothing on disk or on the server will be changed")
        if self.o.check_only:
            self.reporter.note("")
            self.reporter.note("  check-only: read-only detection and validation")

    # -- lock -------------------------------------------------------------- #
    def acquire_lock(self) -> bool:
        if self.o.dry_run or self.o.check_only:
            # Nothing is written in these modes, so there is nothing to exclude.
            return True
        path = self.runtime_dir / "setup.lock"
        try:
            ensure_dir(path.parent, 0o700)
            handle = open(str(path), "a+")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.reporter.error("another setup_Deploy.py run is already in progress (%s)" % path)
            self.reporter.check("setup lock", FAIL,
                                detail="a concurrent installer holds %s" % path,
                                fix="wait for it to finish, then rerun this installer")
            return False
        except OSError as exc:
            self.reporter.warn("could not create the setup lock (%s); continuing without it" % exc)
            return True
        self._lock_handle = handle
        self._lock_path = path
        try:
            handle.seek(0)
            handle.truncate()
            handle.write("pid=%s started=%s\n" % (os.getpid(), now_iso()))
            handle.flush()
        except OSError:
            pass
        self.reporter.log("setup lock acquired: %s" % path)
        return True

    def release_lock(self) -> None:
        if self._lock_handle is not None:
            try:
                fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
                self._lock_handle.close()
            except OSError:
                pass
            self._lock_handle = None

    # ===================================================================== #
    # Stage 1: Git
    # ===================================================================== #
    def stage_git(self) -> None:
        self.reporter.stage("git", "Git checkout")
        detected = self.detected
        if self.o.check_only or self.o.dry_run:
            self._report_git_state()
            return
        if self.o.restore_installer_file and detected.is_git_repo:
            self.restore_installer_file()
            self.detected = self.detect()
            detected = self.detected
        if not detected.is_git_repo:
            self.bootstrap_checkout()
        else:
            self.update_checkout()
        self.verify_repo_contents()

    def restore_installer_file(self) -> None:
        """Explicitly requested: archive the local installer and restore the tracked copy.

        This is the only operation that replaces a modified *tracked* file, it only
        ever touches ``setup_Deploy.py``, and it always keeps a private copy first.
        """
        path = self.target / INSTALLER_NAME
        if not path.is_file():
            self.reporter.warn("--restore-installer-file: %s does not exist" % path)
            return
        if INSTALLER_NAME not in self.detected.tracked_dirty:
            self.reporter.check("Installer file drift", PASS, detail="%s matches the repository copy" % INSTALLER_NAME)
            return
        if not self.prompt.confirm("archive and restore %s from the repository" % path, material=True,
                                   detail="your downloaded copy is copied to %s first" % (self.runtime_dir / "backups")):
            self.prompt.skipped("installer file restore")
            return
        backup = self.backup_file(path, "installer-drift")
        result = self.runner.git("checkout", "--", INSTALLER_NAME, cwd=self.target, mutating=True, timeout=60)
        if result.ok:
            self.reporter.check("Installer file drift", PASS,
                                detail="restored the tracked copy%s" % (" (your copy: %s)" % backup if backup else ""))
        else:
            self.reporter.check("Installer file drift", FAIL, detail=result.brief(),
                                fix="restore it manually: git -C %s checkout -- %s" % (self.target, INSTALLER_NAME))

    def _report_git_state(self) -> None:
        detected = self.detected
        if not detected.is_git_repo:
            self.reporter.check("Git checkout", MANUAL, detail="%s is not a Git checkout yet" % self.target,
                                fix="run without --dry-run/--check-only to bootstrap it")
        else:
            self.reporter.check("Git origin", PASS if normalize_repo_url(detected.origin_url) == normalize_repo_url(self.o.repo_url) else FAIL,
                                detail=redact_url(detected.origin_url or "<none>"),
                                fix="rerun with --set-origin after confirming the repository")
            self.reporter.check("Git branch", PASS if detected.branch == self.o.branch else FAIL,
                                detail="on %s, expected %s" % (detected.branch or "<detached>", self.o.branch),
                                fix="rerun with --switch-branch if switching is intended")
            self.reporter.check("Git working tree", PASS if not detected.dirty else MANUAL,
                                detail="%d change(s)" % len(detected.dirty) if detected.dirty else "clean",
                                fix="resolve local changes before deploying")
            self._report_git_cleanliness_advice()

    def _report_git_cleanliness_advice(self) -> None:
        detected = self.detected
        if not detected.dirty:
            return
        installer_dirty = any(path.startswith(INSTALLER_NAME) for path in detected.tracked_dirty)
        if installer_dirty:
            self.reporter.check(
                "Installer file drift", MANUAL,
                detail="%s differs from the repository copy, which keeps the deploy hook at 409" % INSTALLER_NAME,
                fix="rerun with --restore-installer-file to archive your copy and restore the tracked one "
                    "(or: git -C %s checkout -- %s)" % (self.target, INSTALLER_NAME))
        untracked = [line[3:] for line in detected.dirty if line.startswith("??")]
        if untracked:
            if self._is_home():
                return
            self.reporter.check("Untracked files", WARN,
                                detail="%d untracked file(s); the deploy hook refuses to pull while `git status --porcelain` is non-empty"
                                       % len(untracked),
                                required=False,
                                fix="move them out of the checkout, or let this installer add them to .git/info/exclude")
            if self.prompt.confirm("add the %d untracked path(s) to .git/info/exclude so the hook can pull" % len(untracked),
                                   material=True, detail="\n".join(untracked[:25])):
                added = 0
                for rel in untracked:
                    if self._write_exclude_rule("/%s" % rel, "Untracked file present at setup time; not part of the deployment"):
                        added += 1
                if added and self.may_write():
                    detected.dirty = [line for line in detected.dirty if not line.startswith("??")]
                    self.reporter.ok("excluded %d untracked path(s); the deploy hook's guard is satisfied again" % added)

    def _tracked_paths(self, ref: str) -> "list[str]":
        result = self.runner.git("ls-tree", "-r", "--name-only", ref, cwd=self.target, timeout=60)
        if not result.ok:
            return []
        return [line.strip() for line in result.out.splitlines() if line.strip()]

    def _collisions_with(self, ref: str) -> "list[str]":
        tracked = self._tracked_paths(ref)
        index = self.runner.git("ls-files", cwd=self.target, timeout=60)
        indexed = set(line.strip() for line in index.out.splitlines()) if index.ok else set()
        collisions = []
        for rel in tracked:
            if rel in indexed:
                continue
            if rel == INSTALLER_NAME:
                continue
            candidate = self.target / rel
            if candidate.exists() and not candidate.is_dir():
                collisions.append(rel)
        return collisions

    def _git_shows_untracked(self, rel: str) -> bool:
        status = self.runner.git("status", "--porcelain", "--", rel, cwd=self.target, timeout=30)
        return status.ok and status.out.startswith("??")

    def _write_exclude_rule(self, rule: str, comment: str) -> bool:
        exclude = self.target / ".git" / "info" / "exclude"
        if not rule.startswith("*") and not comment.startswith("Deployment-only"):
            # Already covered by .gitignore or a previous exclude rule: nothing to do.
            check = self.runner.git("check-ignore", "-q", "--", rule.lstrip("/"), cwd=self.target, timeout=20)
            if check.rc == 0:
                return True
        try:
            existing = read_text(exclude) if exclude.exists() else ""
        except OSError:
            existing = ""
        if rule in existing.splitlines():
            return True
        if not self.may_write():
            self.reporter.info("  [dry-run] would add %r to %s" % (rule, exclude))
            return True
        body = existing
        if body and not body.endswith("\n"):
            body += "\n"
        body += "\n# %s\n%s\n" % (comment, rule)
        try:
            exclude.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(exclude, body, 0o644)
        except OSError as exc:
            self.reporter.warn("could not update %s: %s" % (exclude, exc))
            return False
        self.reporter.ok("added %r to .git/info/exclude (%s)" % (rule, comment))
        return True

    def _archive_running_installer(self) -> None:
        """Keep the downloaded/edited installer before a checkout can replace it."""
        local = self.target / INSTALLER_NAME
        if not local.is_file():
            return
        remote_version = self.runner.git("show", "FETCH_HEAD:%s" % INSTALLER_NAME, cwd=self.target, timeout=30)
        try:
            local_text = read_text(local)
        except OSError:
            local_text = ""
        if remote_version.ok and remote_version.out == local_text and local_text:
            return  # identical to the repository copy: nothing worth keeping
        if not self.may_write():
            self.reporter.info("  [dry-run] would archive %s before checking out" % local)
            return
        stamp = now_stamp()
        destination_dir = self.runtime_dir / "backups" / stamp
        ensure_dir(destination_dir, 0o700)
        destination = destination_dir / ("downloaded--%s" % INSTALLER_NAME)
        try:
            shutil.copy2(str(local), str(destination))
            os.chmod(str(destination), 0o600)
        except OSError as exc:
            self.reporter.warn("could not archive %s: %s" % (self.installer_path, exc))
            return
        self.record.setdefault("archived_installers", []).append({"path": str(destination), "at": now_iso()})
        self.reporter.ok("archived the existing %s -> %s" % (INSTALLER_NAME, destination))
        if remote_version.ok and remote_version.out != local_text:
            self.reporter.note("       the repository copy of %s replaces it; the running installer continues from memory"
                               % INSTALLER_NAME)

    def bootstrap_checkout(self) -> None:
        target = self.target
        if not target.exists():
            if not self.prompt.confirm("create %s" % target, material=True):
                self.prompt.skipped("create target directory")
                return
            if self.may_write():
                ensure_dir(target, 0o755)
        self.reporter.info("  %s is not a Git checkout yet" % target)
        confirmed = self.prompt.confirm(
            "initialize a deployment checkout directly in %s" % target,
            material=True,
            detail=("Repository: %s\nBranch: %s\n"
                    "Untracked account files are kept; anything that collides with a tracked AMS path is copied to\n"
                    "%s first." % (self.o.repo_url, self.o.branch, self.runtime_dir / "backups")),
        )
        if not confirmed:
            self.prompt.skipped("Git bootstrap")
            self.reporter.check("Git checkout", MANUAL, detail="bootstrap not confirmed",
                                fix="rerun with --yes in a Bash console")
            return
        init = self.runner.git("init", cwd=target, mutating=True, timeout=60)
        if not init.ok and init.rc != 0:
            self.reporter.check("git init", FAIL, detail=init.brief(), fix="check that %s is writable" % target)
            return
        existing_origin = self.runner.git("remote", "get-url", "origin", cwd=target, timeout=20)
        if existing_origin.ok and existing_origin.out.strip():
            if normalize_repo_url(existing_origin.out) != normalize_repo_url(self.o.repo_url):
                if not (self.o.set_origin and self.prompt.confirm("repoint origin to %s" % self.o.repo_url, material=True)):
                    self.reporter.check("Git origin", FAIL,
                                        detail="origin is %s" % redact_url(existing_origin.out.strip()),
                                        fix="rerun with --set-origin if that remote is wrong")
                    return
                self.runner.git("remote", "set-url", "origin", self.o.repo_url, cwd=target, mutating=True, timeout=30)
        else:
            add = self.runner.git("remote", "add", "origin", self.o.repo_url, cwd=target, mutating=True, timeout=30)
            if not add.ok:
                self.reporter.check("git remote add", FAIL, detail=add.brief(),
                                    fix="remove the stale 'origin' remote manually, then rerun")
                return
        self.reporter.ok("origin -> %s" % redact_url(self.o.repo_url))
        self._report_git_cleanliness_advice()
        if not self._fetch_branch():
            return
        collisions = self._collisions_with("FETCH_HEAD")
        self._archive_running_installer()
        if collisions:
            listing = "\n".join(collisions[:25])
            self.reporter.warn("%d existing file(s) would be replaced by tracked AMS files" % len(collisions))
            for line in listing.splitlines():
                self.reporter.note("       %s" % line)
            confirmed = self.prompt.confirm(
                "continue and let the checkout replace those files (copies are kept in %s)" % (self.runtime_dir / "backups"),
                material=True)
            if not confirmed:
                self.prompt.skipped("Git checkout")
                self.reporter.check("Git checkout", MANUAL,
                                    detail="%d colliding file(s) need a decision" % len(collisions),
                                    fix="move them aside, then rerun")
                return
            if self.may_write():
                for rel in collisions:
                    self.backup_file(target / rel, "collision")
            if not self._is_home():
                # Git refuses to overwrite untracked files; the copies are kept, and the
                # exclude rules are what let the checkout replace them.
                for rel in collisions:
                    self._write_exclude_rule("/%s" % rel,
                                             "Collision handled by setup_Deploy.py (copy in the backup directory)")
        if self._is_home():
            self._write_exclude_rule("*", "Deployment-only home checkout: ignore unrelated untracked account files")
        else:
            self._write_exclude_rule("/%s" % INSTALLER_NAME, "Downloaded installer; the repository copy is authoritative")
        checkout = self.runner.git("checkout", "-b", self.o.branch, "--track", "origin/%s" % self.o.branch,
                                   cwd=target, mutating=True, timeout=180)
        if not checkout.ok:
            brief = checkout.brief()
            self.reporter.check("Git checkout", FAIL, detail=brief,
                                fix="resolve the listed files (never delete them blindly), then rerun")
            return
        self.reporter.ok("checked out a new branch '%s' tracking origin/%s" % (self.o.branch, self.o.branch))
        self.detected = self.detect()
        if self.detected.tracked_dirty:
            self.reporter.warn("checkout is not clean (tracked changes): %s" % ", ".join(self.detected.tracked_dirty[:5]))
        elif self.detected.dirty:
            self.reporter.warn("checkout is not clean (untracked files): %s"
                               % ", ".join(line[3:] for line in self.detected.dirty[:5]))
        else:
            self.reporter.ok("working tree is clean")
        self._report_git_cleanliness_advice()

    def _exclude_setup_artifacts(self) -> None:
        """Keep the installer's own runtime paths from tripping the hook's clean-tree guard."""
        if self.o.check_only or self.o.dry_run or not self.detected.is_git_repo or self._is_home():
            return
        candidates = ["instance", "__pycache__"]
        try:
            relative_venv = str(self.venv_dir.relative_to(self.target)).rstrip("/")
        except ValueError:
            relative_venv = ""
        if relative_venv and not relative_venv.startswith(".."):
            candidates.append(relative_venv)
        added = []
        for rel in candidates:
            if rel and self._git_shows_untracked(rel):
                if self._write_exclude_rule("/" + rel, "Created by setup_Deploy.py (deployment-only checkout)"):
                    added.append(rel)
        if added:
            for rel in added:
                self.reporter.ok("added %s to .git/info/exclude (deployment-only checkout)" % rel)
            self.detected = self.detect()

    def _fetch_branch(self) -> bool:
        timeout = max(self.o.timeout, 180)
        last = Result(argv=["git", "fetch"])
        for attempt in range(self.o.retries + 1):
            last = self.runner.git("fetch", "origin", self.o.branch, cwd=self.target,
                                   mutating=True, timeout=timeout, label="fetch origin/%s" % self.o.branch)
            if last.ok:
                self.reporter.ok("fetched origin/%s" % self.o.branch)
                return True
            message = last.combined.lower()
            transient = any(marker in message for marker in TRANSIENT_MARKERS)
            if not transient or attempt >= self.o.retries:
                break
            self.reporter.warn("git fetch failed (transient): %s" % tail(last.combined, 3))
            self.reporter.info("  retrying in %ss (attempt %d/%d)" % (2 * (attempt + 1), attempt + 2, self.o.retries + 1))
            self.sleep(2 * (attempt + 1))
        detail = tail(last.combined, 6) or "git fetch returned %s" % last.rc
        fix = "check network access to %s and that the branch exists" % repo_owner_name(self.o.repo_url)
        if "could not read Username" in last.combined or "Authentication failed" in last.combined:
            fix = ("the repository is private or credentials are missing; configure read access on the server "
                   "(no credentials are ever stored by this installer)")
        self.reporter.check("git fetch origin/%s" % self.o.branch, FAIL, detail=detail, fix=fix)
        return False

    def update_checkout(self) -> None:
        target = self.target
        detected = self.detected
        origin_ok = normalize_repo_url(detected.origin_url) == normalize_repo_url(self.o.repo_url)
        if not origin_ok:
            if self.o.set_origin and self.prompt.confirm("repoint origin from %s to %s" % (redact_url(detected.origin_url), redact_url(self.o.repo_url)),
                                                         material=True):
                self.runner.git("remote", "set-url", "origin", self.o.repo_url, cwd=target, mutating=True, timeout=30)
                self.reporter.ok("origin repointed to %s" % redact_url(self.o.repo_url))
                origin_ok = True
            else:
                self.reporter.check("Git origin", FAIL,
                                    detail="origin is %s, expected %s" % (detected.origin_url or "<none>", self.o.repo_url),
                                    fix="run with --set-origin after confirming this is the AMS repository")
                return
        if detected.branch != self.o.branch:
            if not self.o.switch_branch:
                self.reporter.check("Git branch", FAIL,
                                    detail="checkout is on '%s', AMS_DEPLOY_BRANCH is '%s'" % (detected.branch or "detached", self.o.branch),
                                    fix="run with --switch-branch if switching this checkout is intended")
                return
            if not self.prompt.confirm("switch the checkout to branch %s" % self.o.branch, material=True):
                self.prompt.skipped("branch switch")
                return
            if self.runner.git("rev-parse", "--verify", self.o.branch, cwd=target, timeout=20).ok:
                switch = self.runner.git("checkout", self.o.branch, cwd=target, mutating=True, timeout=120)
            else:
                switch = self.runner.git("checkout", "-b", self.o.branch, "--track", "origin/%s" % self.o.branch,
                                         cwd=target, mutating=True, timeout=120)
            if not switch.ok:
                self.reporter.check("Git branch switch", FAIL, detail=switch.brief(),
                                    fix="resolve the reported files manually; the installer never discards local changes")
                return
            self.reporter.ok("switched to %s" % self.o.branch)
            self.detected = self.detect()
            detected = self.detected
        self.detected = self.detect()
        detected = self.detected
        if not self._fetch_branch():
            return
        remote_head = self.runner.git("rev-parse", "origin/%s" % self.o.branch, cwd=target, timeout=20)
        local_head = self.runner.git("rev-parse", "HEAD", cwd=target, timeout=20)
        if remote_head.ok and local_head.ok and remote_head.out.strip() == local_head.out.strip():
            self.reporter.check("Git update", PASS, detail="already at %s (up to date with origin/%s)"
                                % (local_head.out.strip()[:10], self.o.branch))
            self._report_git_cleanliness_advice()
            return
        if detected.tracked_dirty:
            self._report_git_cleanliness_advice()
            self.reporter.check("Git update", MANUAL,
                                detail="tracked files are modified, so a fast-forward would refuse: %s"
                                       % ", ".join(detected.tracked_dirty[:5]),
                                fix="review them (git diff), then commit or restore deliberately; this installer never discards changes")
            return
        ahead = self.runner.git("rev-list", "--count", "origin/%s..HEAD" % self.o.branch, cwd=target, timeout=30)
        if ahead.ok and ahead.out.strip() not in ("", "0"):
            self.reporter.check("Git update", FAIL,
                                detail="server checkout is %s commit(s) ahead of origin/%s" % (ahead.out.strip(), self.o.branch),
                                fix="resolve the diverged branch manually (the hook also refuses this); never reset --hard")
            return
        merge = self.runner.git("merge", "--ff-only", "origin/%s" % self.o.branch, cwd=target,
                                mutating=True, timeout=180, label="fast-forward update")
        if merge.ok:
            head = self.runner.git("rev-parse", "--short", "HEAD", cwd=target, timeout=20)
            self.reporter.check("Git update", PASS, detail="fast-forwarded to %s" % (head.out.strip() or "HEAD"))
        else:
            self.reporter.check("Git update", FAIL, detail=merge.brief(),
                                fix="the branch cannot fast-forward; resolve it manually and rerun")
        self.detected = self.detect()

    def verify_repo_contents(self) -> None:
        detected = self.detected
        if not detected.is_git_repo and not self.target.is_dir():
            return
        if detected.missing_paths:
            self.reporter.check("Application files", FAIL, detail="missing: %s" % ", ".join(detected.missing_paths),
                                fix="confirm the deployment branch is merged and up to date")
        else:
            self.reporter.check("Application files", PASS, detail="wsgi.py, deploy_hook.py, app/, models/, templates/, static/ present")
        if not detected.hook_present:
            self.reporter.check("Signed deploy hook", FAIL, detail="%s is missing" % HOOK_FILE,
                                fix="merge the deployment-hook change (PR #5) into %s, then rerun" % self.o.branch)
        elif detected.hook_missing_markers:
            self.reporter.check("Signed deploy hook", FAIL,
                                detail="%s does not look like the supported hook (missing %s)"
                                       % (HOOK_FILE, ", ".join(detected.hook_missing_markers)),
                                fix="update the branch; this installer never writes its own copy of the hook")
        elif detected.hook_missing_recommended:
            self.reporter.check("Signed deploy hook", WARN, required=False,
                                detail="recognised, but these markers are gone: %s" % ", ".join(detected.hook_missing_recommended),
                                fix="review the hook before relying on it")
        else:
            self.reporter.check("Signed deploy hook", PASS, detail="canonical %s present" % HOOK_FILE)
        if not detected.hook_configured_in_wsgi:
            self.reporter.check("Hook mounted by wsgi.py", FAIL,
                                detail="wsgi.py does not import/wrap deploy_hook.wrap_application",
                                fix="the deployment branch must contain the hook wiring; update the branch")
        else:
            self.reporter.check("Hook mounted by wsgi.py", PASS, detail="wsgi.py wraps the hook at /deploy")

    # ===================================================================== #
    # Stage 2: Python environment
    # ===================================================================== #
    def stage_python(self) -> None:
        self.reporter.stage("python", "Python virtualenv and dependencies")
        if self.o.skip_deps:
            self.reporter.check("Dependencies", SKIP, detail="--skip-deps", required=False)
            return
        api = self.ensure_api()
        desired = self.choose_interpreter(api)
        if desired is None:
            self.reporter.check("Python interpreter", FAIL,
                                detail="no usable Python 3 interpreter found",
                                fix="make sure /usr/bin/python3.x is on PATH (PythonAnywhere images ship several)")
            return
        version, path = desired
        self.reporter.check("Python interpreter", PASS,
                            detail="%s -> Python %s" % (path, version_string(version)))
        if self.detected.venv_present and self.detected.venv_version:
            existing = python_version_tuple(self.detected.venv_version)
            if existing[:2] != version[:2]:
                self.reporter.warn("existing virtualenv uses Python %s but %s is selected for the web app"
                                   % (self.detected.venv_version, version_string(version)))
                if self.o.recreate_venv and self.prompt.confirm("rebuild %s with Python %s" % (self.venv_dir, version_string(version)),
                                                                material=True):
                    self._rebuild_venv(path)
                else:
                    self.reporter.check("Virtualenv Python version", MANUAL,
                                        detail="%s is Python %s" % (self.venv_dir, self.detected.venv_version),
                                        fix="rerun with --recreate-venv, or set the Web tab Python version to %s"
                                            % self.detected.venv_version)
                    version, path = existing, self.detected.venv_python
            else:
                self.reporter.check("Virtualenv", PASS, detail="reusing %s (Python %s)" % (self.venv_dir, self.detected.venv_version))
        if not self.venv_python.is_file():
            self._create_venv(path, version)
        if not self.venv_python.is_file():
            return
        if self.o.upgrade_pip:
            self._pip(["install", "--upgrade", "pip", "setuptools", "wheel"], timeout=max(120, self.o.timeout * 2))
        self._install_requirements()
        self._validate_environment()

    def choose_interpreter(self, api: "PythonAnywhereAPI | None") -> "tuple[tuple[int, ...], str] | None":
        if self.o.python:
            resolved = which(self.o.python)
            if not resolved:
                self.reporter.warn("--python %s not found on PATH" % self.o.python)
                return None
            probe = self.runner.run([resolved, "-c", "import sys;print('%d.%d.%d' % sys.version_info[:3])"], timeout=30)
            version = python_version_tuple(probe.out.strip()) if probe.ok else ()
            return (version or (0, 0), resolved)
        if api is not None and not self.web_app_python_version:
            config = api.get_webapp()
            if config.ok:
                try:
                    payload = config.json()
                    if isinstance(payload, dict):
                        self.web_app = payload
                        self.web_app_python_version = str(payload.get("python_version") or "")
                except ValueError:
                    pass
        candidates = self.platform.interpreters
        if not candidates:
            return None
        if self.web_app_python_version:
            wanted = python_version_tuple(self.web_app_python_version)
            for version, path, _resolved in candidates:
                if version[:2] == wanted[:2]:
                    return version, path
            self.reporter.warn("the web app uses Python %s, which is not available in this console (see the Web tab)"
                               % self.web_app_python_version)
        return candidates[0][0], candidates[0][1]

    def _rebuild_venv(self, interpreter: str) -> None:
        old = self.venv_dir
        stamp = now_stamp()
        archive = old.parent / ("%s.old-%s" % (old.name, stamp))
        try:
            if self.may_write():
                shutil.move(str(old), str(archive))
                self.reporter.ok("moved the old virtualenv to %s" % archive)
        except OSError as exc:
            self.reporter.warn("could not move the old virtualenv aside: %s" % exc)
        self.o.recreate_venv = False

    def _create_venv(self, interpreter: str, version: "tuple[int, ...]") -> None:
        self.reporter.info("  creating %s with %s" % (self.venv_dir, interpreter))
        result = self.runner.run([interpreter, "-m", "venv", str(self.venv_dir)], mutating=True,
                                 timeout=max(180, self.o.timeout * 3), label="create virtualenv")
        if not result.ok or not self.venv_python.is_file():
            self.reporter.check("Virtualenv", FAIL, detail=result.brief(),
                                fix="create it manually: %s -m venv %s" % (interpreter, self.venv_dir))
            return
        self.reporter.check("Virtualenv", PASS, detail="created %s with %s (Python %s)"
                            % (self.venv_dir, interpreter, version_string(version)))
        self.record["venv"] = {"path": str(self.venv_dir), "python": interpreter, "version": version_string(version)}

    def _pip(self, args: "Sequence[str]", *, timeout: int, attempts: "int | None" = None) -> Result:
        attempts = (self.o.retries if attempts is None else attempts) + 1
        last = Result(argv=["pip", *args])
        for attempt in range(attempts):
            last = self.runner.run([str(self.venv_python), "-m", "pip", *args], mutating=True,
                                   timeout=timeout, label="pip %s" % args[0])
            if last.ok:
                return last
            message = last.combined.lower()
            if not any(marker in message for marker in TRANSIENT_MARKERS) or attempt + 1 >= attempts:
                return last
            self.reporter.warn("pip hit a transient error; retrying (attempt %d/%d)" % (attempt + 2, attempts))
            self.sleep(3 * (attempt + 1))
        return last

    def _install_requirements(self) -> None:
        if not self.requirements.is_file():
            self.reporter.check("Dependencies", FAIL, detail="requirements.txt not found in %s" % self.target,
                                fix="make sure the checkout is complete")
            return
        digest = sha256_file(self.requirements)
        previous = str(self.detected.state.get("requirements_sha256") or "")
        if previous == digest and self.detected.venv_present:
            self.reporter.check("Dependencies", INFO, detail="requirements.txt unchanged since the last install; verifying instead",
                                required=False)
        args = ["install", "--disable-pip-version-check", "--no-input", "--retries", "2",
                "--timeout", "60", "-r", str(self.requirements)]
        self.reporter.info("  installing requirements into %s (timeout %ss per attempt)" % (self.venv_dir, self.o.pip_timeout))
        result = self._pip(args, timeout=self.o.pip_timeout)
        if result.ok:
            self.reporter.check("Dependencies", PASS, detail="pip install -r requirements.txt succeeded")
            self.record["requirements_sha256"] = digest
        else:
            self.reporter.check("Dependencies", FAIL, detail=tail(result.combined, 8),
                                fix="fix the reported error, then rerun; or run manually: %s -m pip install -r %s"
                                    % (self.venv_python, self.requirements))

    def _validate_environment(self) -> None:
        code = ("import importlib, sys\n"
                "mods = ['flask', 'flask_login', 'flask_sqlalchemy', 'sqlalchemy', 'pandas', 'numpy', 'openpyxl', 'reportlab']\n"
                "missing = []\n"
                "for name in mods:\n"
                "    try:\n"
                "        importlib.import_module(name)\n"
                "    except Exception as exc:\n"
                "        missing.append('%s (%s)' % (name, exc))\n"
                "print('MISSING:' + '; '.join(missing) if missing else 'IMPORTS-OK')\n")
        result = self.runner.run([str(self.venv_python), "-c", code], timeout=max(120, self.o.timeout * 2))
        output = result.out.strip()
        if result.ok and "IMPORTS-OK" in output:
            self.reporter.check("Dependencies importable", PASS,
                                detail="flask, sqlalchemy, pandas, numpy, openpyxl, reportlab import cleanly")
        else:
            self.reporter.check("Dependencies importable", FAIL,
                                detail=tail(output or result.combined, 5),
                                fix="install the missing packages into %s, then rerun" % self.venv_dir)
        weasy = self.runner.run([str(self.venv_python), "-c", "import weasyprint;print('weasyprint-ok')"],
                                timeout=max(120, self.o.timeout * 2))
        if weasy.ok and "weasyprint-ok" in weasy.out:
            self.reporter.check("WeasyPrint", PASS, detail="native PDF engine importable", required=False)
        else:
            self.reporter.check("WeasyPrint", WARN, required=False,
                                detail="not importable here (missing pango/cairo is normal on shared hosts); PDFs fall back to ReportLab",
                                fix="install the system libraries only if you need WeasyPrint output")
        self._deps_validated = True

    # ===================================================================== #
    # Stage 3: secret
    # ===================================================================== #
    def stage_secret(self) -> None:
        self.reporter.stage("secret", "Deployment secret")
        path = self.secret_path
        if self.may_write():
            ensure_dir(path.parent, 0o700)
        existing = ""
        if path.is_file():
            try:
                existing = read_text(path).strip()
            except OSError:
                existing = ""
        rotate = bool(self.o.rotate_secret)
        if existing and is_secret_value_valid(existing):
            self.secret_value = existing
            self.redactor.add(existing)
            if rotate:
                self.reporter.warn("--rotate-secret requested; the GitHub webhook must be updated in the same step")
                if self.prompt.confirm("rotate %s now" % path, material=True):
                    self._write_new_secret(path, rotate=True)
                else:
                    self.prompt.skipped("secret rotation")
                    self.reporter.check("Secret file", PASS,
                                        detail="keeping the existing secret (%s)" % fingerprint(existing))
            else:
                if self.may_write() and file_mode(path) & 0o777 != 0o600:
                    try:
                        os.chmod(str(path), 0o600)
                        self.reporter.ok("tightened permissions on %s to 0600" % path)
                    except OSError as exc:
                        self.reporter.warn("could not chmod %s: %s" % (path, exc))
                self.reporter.check("Secret file", PASS,
                                    detail="keeping the existing %s (%s)" % (path, fingerprint(existing)))
        elif existing:
            self.reporter.check("Secret file", WARN, required=False,
                                detail="%s exists but does not look like a webhook secret" % path,
                                fix="rerun with --rotate-secret to replace it (GitHub must be updated at the same time)")
            self.secret_value = existing
            self.redactor.add(existing)
            if rotate and self.prompt.confirm("replace the unusable secret in %s" % path, material=True):
                self._write_new_secret(path, rotate=True)
        elif self.o.check_only or self.o.dry_run:
            self.reporter.check("Secret file", MANUAL, detail="%s does not exist yet" % path,
                                fix="rerun without --check-only/--dry-run to create it")
        else:
            self._write_new_secret(path)
        self._check_exposed_secret()
        self._exclude_setup_artifacts()

    def _write_new_secret(self, path: Path, rotate: bool = False) -> None:
        value = secrets.token_hex(32)
        previous_fingerprint = fingerprint(self.secret_value) if self.secret_value else ""
        if not self.may_write():
            self.reporter.check("Secret file", MANUAL, detail="would create %s (%s)" % (path, fingerprint(value)),
                                fix="rerun without --dry-run")
            return
        try:
            atomic_write(path, value + "\n", 0o600)
        except OSError as exc:
            self.reporter.check("Secret file", FAIL, detail=str(exc), fix="check that %s is writable" % path.parent)
            return
        self.secret_value = value
        self.redactor.add(value)
        detail = "created %s (%s)" % (path, fingerprint(value))
        if rotate and previous_fingerprint:
            detail = "rotated %s (%s -> %s)" % (path, previous_fingerprint, fingerprint(value))
        self.reporter.check("Secret file", PASS, detail=detail)
        self.record["secret_fingerprint"] = fingerprint(value)
        self.record["secret_path"] = str(path)

    def _exposed_committed_secrets(self) -> "set[str]":
        target = self.target
        log = self.runner.git("log", "--all", "--full-history", "--format=%H", "-n", "5", "--", EXPOSED_SECRET_REL,
                              cwd=target, timeout=30)
        values: "set[str]" = set()
        if not log.ok:
            return values
        for commit in [line.strip() for line in log.out.splitlines() if line.strip()]:
            blob = self.runner.git("show", "%s:%s" % (commit, EXPOSED_SECRET_REL), cwd=target, timeout=30)
            if blob.ok:
                value = blob.out.strip()
                if value:
                    values.add(value)
        return values

    def _check_exposed_secret(self) -> None:
        target = self.target
        stale = target / EXPOSED_SECRET_REL
        committed: "set[str]" = set()
        if self.detected.is_git_repo:
            committed = self._exposed_committed_secrets()
        problems = []
        if stale.is_file():
            try:
                stale_value = read_text(stale).strip()
            except OSError:
                stale_value = ""
            if stale_value and self.secret_value and stale_value == self.secret_value:
                problems.append("the active secret is also in the repository root file %s" % EXPOSED_SECRET_REL)
            else:
                problems.append("a stale root-level %s exists in the working tree" % EXPOSED_SECRET_REL)
        if self.secret_value and self.secret_value in committed:
            problems.append("the active secret matches a value that was committed to Git history")
        if not problems:
            if self.detected.is_git_repo:
                self.reporter.check("Exposed secret", PASS, detail="not equal to the previously committed root-level secret")
            return
        for problem in problems:
            self.reporter.check("Exposed secret", WARN, required=False, detail=problem,
                                fix="rotate it: rerun with --rotate-secret, then paste the new value into GitHub")
        if stale.exists() and self.prompt.confirm("quarantine the stale root-level %s into the backup directory" % EXPOSED_SECRET_REL,
                                                  material=True):
            self.backup_file(stale, "stale-root-secret")
            if self.may_write():
                try:
                    stale.unlink()
                    self.reporter.ok("removed %s (a copy is in the backup directory)" % stale)
                except OSError as exc:
                    self.reporter.warn("could not remove %s: %s" % (stale, exc))
        self.record.setdefault("warnings", []).append("exposed-secret: " + "; ".join(problems))

    # ===================================================================== #
    # Stage 4: WSGI configuration
    # ===================================================================== #
    def render_managed_block(self) -> str:
        lines = [
            MANAGED_BEGIN,
            "# Generated by %s v%s — rerunning the installer replaces this block." % (INSTALLER_NAME, VERSION),
            "# Keep host-specific setup outside the markers; nothing else here is touched.",
            "import os",
            "import sys",
            "",
            "AMS_PROJECT_DIR = %r" % str(self.target),
            "if AMS_PROJECT_DIR not in sys.path:",
            "    sys.path.insert(0, AMS_PROJECT_DIR)",
            "",
            "# The deploy hook touches exactly this file to request a web-app reload; it",
            "# must be the file shown under Web -> WSGI configuration file.",
            'os.environ["AMS_WSGI_FILE"] = %r' % self.detected.wsgi_file,
            'os.environ["AMS_DEPLOY_BRANCH"] = %r' % self.o.branch,
            'os.environ.setdefault("SQLITE_JOURNAL_MODE", "DELETE")',
            'os.environ.setdefault("AMS_HTTPS", "1")',
        ]
        if self.env.get("AMS_DEPLOY_SECRET_FILE"):
            lines.append('os.environ.setdefault("AMS_DEPLOY_SECRET_FILE", %r)' % str(self.secret_path))
        lines += ["", "from wsgi import application  # noqa: E402", MANAGED_END]
        return "\n".join(lines) + "\n"

    def _render_wsgi(self, text: str, adopt: bool) -> "tuple[str, int, list[str]]":
        """Return (new_text, removed_block_count, disabled_lines)."""
        text = normalize_wsgi_source(text)
        kept: "list[str]" = []
        removed_blocks = 0
        inside = False
        for line in text.split("\n"):
            stripped = line.strip()
            if stripped == MANAGED_BEGIN:
                inside = True
                removed_blocks += 1
                continue
            if stripped == MANAGED_END:
                inside = False
                continue
            if inside:
                continue
            kept.append(line)
        disabled: "list[str]" = []
        result: "list[str]" = []
        for line in kept:
            stripped = line.strip()
            unmanaged = (not stripped.startswith("#")
                         and any(marker in stripped for marker in WSGI_WRAP_MARKERS))
            if unmanaged and adopt:
                result.append("# disabled by %s (superseded by the managed block below): %s" % (INSTALLER_NAME, line))
                disabled.append(line.strip())
            else:
                result.append(line)
        body = "\n".join(result).rstrip("\n")
        combined = (body + "\n\n" if body else "") + self.render_managed_block()
        return combined, removed_blocks, disabled

    def stage_wsgi(self) -> None:
        self.reporter.stage("wsgi", "PythonAnywhere WSGI configuration")
        detected = self.detected
        path = Path(detected.wsgi_file) if detected.wsgi_file else None
        if self.o.skip_wsgi:
            self.reporter.check("WSGI configuration", SKIP, detail="--skip-wsgi", required=False)
            return
        if path is None:
            self.reporter.check("WSGI configuration", MANUAL, detail="no WSGI file path could be determined",
                                fix="pass --wsgi-file with the exact path shown in the Web tab")
            return
        if not detected.wsgi_exists and not self.o.create_wsgi_file:
            self._print_wsgi_manual(path)
            return
        if not detected.wsgi_exists and self.o.create_wsgi_file and not self.o.wsgi_file:
            self.reporter.check("WSGI configuration", MANUAL,
                                detail="--create-wsgi-file needs the exact path from the Web tab",
                                fix="pass --wsgi-file /var/www/<the file shown in the Web tab> (this installer never "
                                    "guesses a new file name)")
            self._print_wsgi_manual(path)
            return
        if detected.wsgi_foreign and not detected.wsgi_has_managed:
            self.reporter.check("WSGI configuration", FAIL,
                                detail="%s looks like another application's WSGI file" % path,
                                fix="point the installer at the AMS WSGI file with --wsgi-file; nothing was changed")
            return
        text = read_text(path) if detected.wsgi_exists else ""
        if any(token in text for token in ('AMS_HTTPS", "0', "AMS_HTTPS', '0", 'AMS_HTTPS"] = "0')):
            self.reporter.check("Existing AMS_HTTPS override", WARN, required=False,
                                detail="the WSGI file sets AMS_HTTPS to a non-1 value",
                                fix="remove that line if this site is served over HTTPS")
        needs_adopt = detected.wsgi_has_unmanaged_ams
        adopt = False
        if needs_adopt:
            adopt = bool(self.o.adopt_wsgi) or self.prompt.confirm(
                "replace the hand-written AMS import in %s with the managed block" % path,
                material=True,
                detail="the old line is kept as a comment; the original file is backed up first")
            if not adopt:
                self.prompt.skipped("WSGI adoption")
                self.reporter.check("WSGI configuration", MANUAL,
                                    detail="a hand-written AMS import is present in %s" % path,
                                    fix="rerun with --adopt-wsgi to convert it into the managed block")
                return
        new_text, removed, disabled = self._render_wsgi(text, adopt)
        unchanged = detected.wsgi_exists and new_text == text
        if unchanged:
            self.reporter.check("WSGI configuration", PASS,
                                detail="managed block already present and up to date in %s" % path)
            self.record["wsgi_file"] = str(path)
            return
        if self.o.check_only or self.o.dry_run:
            self.reporter.check("WSGI configuration", MANUAL,
                                detail="would %s %s" % ("update the managed block in" if detected.wsgi_has_managed
                                                        else "add the managed block to", path),
                                fix="rerun without --dry-run/--check-only to apply it")
            return
        self.reporter.warn("writing %s and reloading restarts the web app; AMS applies any pending SQL migrations on startup"
                           % path)
        confirmed = self.prompt.confirm(
            "%s the managed AMS block in %s" % ("update" if detected.wsgi_has_managed else "add", path),
            material=True,
            detail="backup: %s\nblock: %s" % (self.runtime_dir / "backups", path))
        if not confirmed:
            self.prompt.skipped("WSGI update")
            self.reporter.check("WSGI configuration", MANUAL, detail="not confirmed",
                                fix="rerun with --yes or edit the Web-tab WSGI file with the block printed below")
            self._print_wsgi_manual(path)
            return
        try:
            ast.parse(new_text)
        except SyntaxError as exc:
            self.reporter.check("WSGI configuration", FAIL, detail="generated config is not valid Python: %s" % exc,
                                fix="report this as a bug; nothing was written")
            return
        if detected.wsgi_exists:
            self.backup_file(path, "wsgi")
            mode = file_mode(path) or 0o644
        else:
            mode = 0o644
        try:
            atomic_write(path, new_text, mode)
        except OSError as exc:
            self.reporter.check("WSGI configuration", FAIL, detail=str(exc),
                                fix="check that %s is writable by this account" % path)
            return
        for line in disabled:
            self.reporter.note("       disabled old import: %s" % line)
        self.reporter.check("WSGI configuration", PASS,
                            detail="%s managed block in %s%s" % ("replaced" if removed else "added",
                                                                 path,
                                                                 "" if not disabled else " (1 hand-written import disabled)"))
        self.record["wsgi_file"] = str(path)
        self.detected.wsgi_exists = True
        self.detected.wsgi_has_managed = True
        self.detected.wsgi_file = str(path)

    def _print_wsgi_manual(self, path: Path) -> None:
        block = self.render_managed_block()
        target_block = self.runtime_dir / "wsgi_ams_block.py"
        target_full = self.runtime_dir / "wsgi_full_config.py"
        header = ("# AMS managed block for the PythonAnywhere WSGI file.\n"
                  "# Paste this into the file shown under Web -> WSGI configuration file (expected: %s).\n"
                  "# The install itself never guesses that path; pass --wsgi-file to automate it.\n\n" % path)
        if self.may_write():
            ensure_dir(self.runtime_dir, 0o700)
            atomic_write(target_block, header + block, 0o600)
            atomic_write(target_full, header + block, 0o600)
        self.reporter.check("WSGI configuration", MANUAL,
                            detail="%s does not exist (the Web tab shows the exact name for this domain)" % path,
                            fix="open Web -> WSGI configuration file, paste the content of %s, save, then click Reload"
                                % target_block)
        if not self.o.quiet:
            self.reporter.section("WSGI content to paste")
            for line in block.splitlines():
                self.reporter.note("  " + line)

    # ===================================================================== #
    # Stage 5: PythonAnywhere API (optional)
    # ===================================================================== #
    def _resolve_token(self, env_names: "Sequence[str]", file_option: "Path | None",
                       label: str, prompt_text: str, allow_prompt: bool = True) -> str:
        for name in env_names:
            value = (self.env.get(name) or "").strip()
            if value:
                self.reporter.log("%s token read from $%s" % (label, name))
                return value
        if file_option is not None:
            try:
                value = read_text(file_option).strip()
            except OSError as exc:
                self.reporter.warn("could not read %s: %s" % (file_option, exc))
                return ""
            mode = file_mode(file_option)
            if mode & 0o077:
                self.reporter.warn("%s is readable by others (mode %o); tighten it to 0600" % (file_option, mode & 0o777))
            return value
        if allow_prompt and self.prompt.interactive:
            return self.prompt.hidden(prompt_text)
        return ""

    def _existing_github_token(self) -> str:
        if self.github_token:
            return self.github_token
        token = self._resolve_token(("GITHUB_TOKEN", "GH_TOKEN"), self.o.github_token_file,
                                    "GitHub", "", allow_prompt=False)
        if token:
            self.redactor.add(token)
        return token

    def _github_token_from_gh(self) -> str:
        """Read a token from the gh CLI without ever letting it reach the log.

        This is the one place that must not use the command runner: the runner
        records command output, and that output *is* the secret.
        """
        gh = which("gh")
        if not gh:
            return ""
        try:
            proc = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, timeout=30,
                                  env=self.runner._env({"GIT_TERMINAL_PROMPT": "0"}))
        except (OSError, subprocess.SubprocessError):
            return ""
        token = (proc.stdout or "").strip()
        if proc.returncode == 0 and token:
            self.redactor.add(token)
            self.reporter.log("GitHub token obtained from the gh CLI (value not logged)")
            return token
        return ""

    def ensure_api(self) -> "PythonAnywhereAPI | None":
        if self.api is not None:
            return self.api
        if not self.o.use_api:
            return None
        token = self._resolve_token(("API_TOKEN", "PA_API_TOKEN"), self.o.api_token_file,
                                    "PythonAnywhere", "PythonAnywhere API token (hidden, never logged): ")
        if not token:
            return None
        self.redactor.add(token)
        username = self.o.pa_username or self.platform.user
        api = PythonAnywhereAPI(username=username, token=token, host=self.o.pa_host,
                                http=self.http, reporter=self.reporter, domain=self.o.domain)
        if not api.resolve_host():
            self.reporter.warn("the PythonAnywhere API token was not accepted (or the API host is unreachable); "
                               "continuing without API automation")
            return None
        self.reporter.ok("PythonAnywhere API authenticated (host %s, user %s)" % (api.host, username))
        self.redactor.add(token)
        self.api = api
        return api

    def _venv_pa_python_version(self) -> str:
        version = python_version_tuple(self.detected.venv_version or "")
        if not version:
            proc = self.runner.run([str(self.venv_python), "-c", "import sys;print('%d.%d' % sys.version_info[:2])"],
                                   timeout=30)
            version = python_version_tuple(proc.out.strip()) if proc.ok else ()
        if not version:
            return ""
        return "python%d%d" % (version[0], version[1])

    def stage_pythonanywhere(self) -> bool:
        """Configure the Web tab through the API. Returns True when a web app was created."""
        self.reporter.stage("pythonanywhere", "PythonAnywhere Web tab")
        if not self.o.use_api:
            self.reporter.check("PythonAnywhere API", SKIP, required=False, detail="--no-api")
            self._print_web_tab_manual("API automation was disabled with --no-api")
            return False
        api = self.ensure_api()
        if api is None:
            self.reporter.check("PythonAnywhere API", MANUAL,
                                detail="no API token available (set $API_TOKEN or pass --api-token-file)",
                                fix="the exact Web-tab steps are printed below; they take about two minutes")
            self._print_web_tab_manual("no authenticated PythonAnywhere API session")
            return False
        webapp = api.get_webapp()
        python_version = self._venv_pa_python_version()
        created_webapp = False
        if webapp.status == 404:
            self.reporter.check("Web app exists", MANUAL, detail="no web app for %s yet" % self.o.domain,
                                fix="create it in the Web tab, or rerun with --yes to let the installer create it")
            if not python_version:
                self.reporter.check("Web app creation", MANUAL,
                                    detail="the virtualenv's Python version could not be determined",
                                    fix="create the web app in the Web tab, then rerun")
                self._print_web_tab_manual("the web app does not exist yet")
                return False
            answer = self.prompt.confirm("create the web app %s (manual configuration, %s)" % (self.o.domain, python_version),
                                         material=True)
            if not answer:
                self._print_web_tab_manual("the web app does not exist yet")
                return False
            created = api.create_webapp(python_version)
            if created.ok:
                self.reporter.check("Web app created", PASS, detail="%s (%s)" % (self.o.domain, python_version))
                created_webapp = True
                self.detect()
                webapp = api.get_webapp()
            else:
                self.reporter.check("Web app created", MANUAL,
                                    detail="API returned %s %s" % (created.status, created.brief),
                                    fix="create it in the Web tab (Add a new web app -> Manual configuration)")
                self._print_web_tab_manual("web-app creation through the API did not succeed")
                return False
        if not webapp.ok:
            self.reporter.check("Web app config", MANUAL,
                                detail="API returned %s %s" % (webapp.status, webapp.brief),
                                fix="check the token's permissions, then set the Web tab by hand")
            self._print_web_tab_manual("the web app configuration could not be read")
            return False
        try:
            payload = webapp.json()
            self.web_app = payload if isinstance(payload, dict) else {}
        except ValueError:
            self.web_app = {}
        self.web_app_python_version = str((self.web_app or {}).get("python_version") or "")
        current_source = str((self.web_app or {}).get("source_directory") or "")
        current_venv = str((self.web_app or {}).get("virtualenv_path") or "")
        changes: "dict[str, Any]" = {}
        if current_source != str(self.target):
            changes["source_directory"] = str(self.target)
        if current_venv != str(self.venv_dir):
            changes["virtualenv_path"] = str(self.venv_dir)
        if python_version and self.web_app_python_version and python_version != self.web_app_python_version:
            changes["python_version"] = python_version
        if changes:
            detail = ", ".join("%s=%s" % (key, value) for key, value in sorted(changes.items()))
            if self.prompt.confirm("update the web app configuration", material=True, detail=detail):
                updated = api.patch_webapp(changes)
                if updated.ok:
                    self.reporter.check("Web app config", PASS, detail="updated: %s" % detail)
                else:
                    self.reporter.check("Web app config", MANUAL,
                                        detail="API returned %s %s" % (updated.status, updated.brief),
                                        fix="set these values in the Web tab: %s" % detail)
                    self._print_web_tab_manual("the configuration update was rejected")
            else:
                self.prompt.skipped("web app configuration update")
                self.reporter.check("Web app config", MANUAL, detail="needs: %s" % detail,
                                    fix="set these values in the Web tab")
        else:
            self.reporter.check("Web app config", PASS,
                                detail="source_directory and virtualenv_path already correct (%s)" % str(self.target))
        if python_version and self.web_app_python_version and python_version != self.web_app_python_version:
            self.reporter.check("Python version match", MANUAL,
                                detail="Web tab uses %s, virtualenv is %s" % (self.web_app_python_version, python_version),
                                fix="the web app Python version must match the virtualenv (Web tab -> Python version)")
        elif python_version:
            self.reporter.check("Python version match", PASS,
                                detail="Web tab and virtualenv both use %s" % python_version)
        self._configure_static(api)
        self._web_app_ready = True
        self.record["pythonanywhere"] = {"domain": self.o.domain, "host": api.host,
                                         "web_app_python_version": self.web_app_python_version,
                                         "configured_at": now_iso()}
        return created_webapp

    def _configure_static(self, api: "PythonAnywhereAPI") -> None:
        wanted_url = "/static/"
        wanted_path = str(self.target / "static")
        response = api.static_mappings()
        if not response.ok:
            self.reporter.check("Static files mapping", MANUAL,
                                detail="API returned %s %s" % (response.status, response.brief),
                                fix="add in the Web tab: URL %s -> %s" % (wanted_url, wanted_path))
            return
        try:
            mappings = response.json()
        except ValueError:
            mappings = []
        if not isinstance(mappings, list):
            mappings = []
        match = None
        for item in mappings:
            if isinstance(item, dict) and str(item.get("url") or "") == wanted_url:
                match = item
                break
        if match is None:
            if self.prompt.confirm("add the static files mapping", material=True,
                                   detail="URL %s -> %s" % (wanted_url, wanted_path)):
                created = api.create_static(wanted_url, wanted_path)
                if created.ok:
                    self.reporter.check("Static files mapping", PASS, detail="created %s -> %s" % (wanted_url, wanted_path))
                else:
                    self.reporter.check("Static files mapping", MANUAL,
                                        detail="API returned %s %s" % (created.status, created.brief),
                                        fix="add in the Web tab: URL %s -> %s" % (wanted_url, wanted_path))
            else:
                self.prompt.skipped("static files mapping")
            return
        current_path = str(match.get("path") or "")
        if current_path == wanted_path:
            self.reporter.check("Static files mapping", PASS, detail="%s -> %s" % (wanted_url, current_path))
            return
        if self.prompt.confirm("update the static files mapping", material=True,
                               detail="%s -> %s (currently %s)" % (wanted_url, wanted_path, current_path)):
            updated = api.update_static(match.get("id"), wanted_url, wanted_path)
            if updated.ok:
                self.reporter.check("Static files mapping", PASS, detail="updated to %s" % wanted_path)
            else:
                self.reporter.check("Static files mapping", MANUAL,
                                    detail="API returned %s %s" % (updated.status, updated.brief),
                                    fix="set in the Web tab: %s -> %s" % (wanted_url, wanted_path))
        else:
            self.prompt.skipped("static files mapping update")
            self.reporter.check("Static files mapping", MANUAL, detail="mapping points at %s" % current_path,
                                fix="set in the Web tab: %s -> %s" % (wanted_url, wanted_path))

    def _reload_web_app(self, api: "PythonAnywhereAPI") -> None:
        self.reporter.warn("reloading the web app restarts it and applies any pending AMS SQL migrations")
        if not self.prompt.confirm("reload %s now" % self.o.domain, material=True):
            self.prompt.skipped("web app reload")
            self.reporter.check("Web app reload", MANUAL, detail="not reloaded in this run",
                                fix="click Reload in the Web tab, or rerun with --yes")
            return
        response = api.reload_webapp()
        if response.ok:
            self.reporter.check("Web app reload", PASS, detail="reload requested through the API")
            self.record["last_reload_at"] = now_iso()
        else:
            self.reporter.check("Web app reload", MANUAL,
                                detail="API returned %s %s" % (response.status, response.brief),
                                fix="click Reload in the Web tab")

    def _print_web_tab_manual(self, reason: str) -> None:
        python_version = self._venv_pa_python_version() or "the same version as the virtualenv"
        self.reporter.section("Web tab steps (manual)")
        self.reporter.note("  Reason: %s" % reason)
        self.reporter.note("  1. Web -> Add a new web app -> Manual configuration -> %s (only if %s has no web app yet)"
                           % (python_version, self.o.domain))
        self.reporter.note("  2. Source code:       %s" % self.target)
        self.reporter.note("  3. Working directory: %s" % self.target)
        self.reporter.note("  4. Virtualenv:        %s" % self.venv_dir)
        self.reporter.note("  5. Static files:      URL /static/  ->  %s" % (self.target / "static"))
        self.reporter.note("  6. WSGI configuration file: open it and paste the managed block from %s"
                           % (self.runtime_dir / "wsgi_ams_block.py"))
        self.reporter.note("  7. Click Reload.")

    # ===================================================================== #
    # Stage 6: GitHub webhook
    # ===================================================================== #
    def _normalize_hook_url(self, url: str) -> str:
        return (url or "").strip().rstrip("/").lower()

    def stage_webhook(self) -> None:
        self.reporter.stage("webhook", "GitHub push webhook")
        self.payload_url = "https://%s%s" % (self.o.domain, DEPLOY_PATH)
        if self.o.skip_webhook:
            self.reporter.check("GitHub webhook", SKIP, required=False, detail="--skip-webhook")
            return
        if not self.secret_value:
            self.reporter.check("GitHub webhook", MANUAL, detail="no secret is available yet",
                                fix="create instance/deploy_secret.txt first (rerun without --skip-* options)")
            self._print_webhook_manual("the deployment secret is missing")
            return
        self.github_token = self._resolve_token(("GITHUB_TOKEN", "GH_TOKEN"), self.o.github_token_file,
                                                "GitHub", "GitHub token (hidden, never logged): ") or self._github_token_from_gh()
        if not self.github_token:
            self.reporter.check("GitHub webhook", MANUAL,
                                detail="no GitHub token available, so nothing was created or verified",
                                fix="follow the manual steps below, or rerun with $GITHUB_TOKEN / --github-token-file")
            self._print_webhook_manual("no GitHub authentication is available")
            return
        self.redactor.add(self.github_token)
        slug = repo_owner_name(self.o.repo_url)
        api = GitHubAPI(slug=slug, token=self.github_token, http=self.http, reporter=self.reporter)
        hooks, response = api.list_hooks()
        if not response.ok:
            detail = "GitHub returned %s %s" % (response.status, response.brief)
            fix = "the token needs the 'repo' (or 'admin:repo_hook') scope"
            if response.status == 404:
                fix = "the token cannot see %s; check the repository and token permissions" % slug
            self.reporter.check("GitHub webhook", MANUAL, detail=detail, fix=fix)
            self._print_webhook_manual("the GitHub API refused the webhook request")
            return
        matches = [hook for hook in hooks
                   if self._normalize_hook_url(str((hook.get("config") or {}).get("url") or "")) == self._normalize_hook_url(self.payload_url)]
        if len(matches) > 1:
            self.reporter.check("GitHub webhook", MANUAL,
                                detail="%d webhooks already point at %s" % (len(matches), self.payload_url),
                                fix="remove the duplicates in GitHub -> Settings -> Webhooks (no new one was created)")
            return
        if not matches:
            if not self.prompt.confirm("create the webhook for %s" % self.payload_url, material=True,
                                       detail="content type application/json, push events only, SSL verification on"):
                self.prompt.skipped("webhook creation")
                self._print_webhook_manual("creating the webhook was not confirmed")
                return
            created = api.create_hook(hook_payload(self.payload_url, self.secret_value))
            if created.ok:
                try:
                    hook_id = created.json().get("id")
                except ValueError:
                    hook_id = None
                self.reporter.check("GitHub webhook", PASS, detail="created hook %s -> %s" % (hook_id, self.payload_url))
                self._remember_webhook(hook_id, verified=self._verify_hook_delivery(api, hook_id))
            else:
                self.reporter.check("GitHub webhook", MANUAL,
                                    detail="GitHub returned %s %s" % (created.status, created.brief),
                                    fix="use the manual steps below (they are two fields in the GitHub UI)")
                self._print_webhook_manual("the GitHub API refused to create the webhook")
            return
        hook = matches[0]
        hook_id = hook.get("id")
        recorded = self.detected.state.get("webhook") or {}
        same_secret = (str(recorded.get("id")) == str(hook_id)
                       and str(recorded.get("secret_fingerprint") or "") == fingerprint(self.secret_value))
        url_ok = self._normalize_hook_url(str((hook.get("config") or {}).get("url") or "")) == self._normalize_hook_url(self.payload_url)
        content_ok = str((hook.get("config") or {}).get("content_type") or "") == "application/json"
        events = hook.get("events") or []
        push_only = events == ["push"]
        if same_secret and url_ok and content_ok and push_only and hook.get("active", True):
            self.reporter.check("GitHub webhook", PASS, detail="existing hook %s matches the recorded secret fingerprint" % hook_id)
            self.record["webhook"] = recorded
            return
        detail_bits = []
        if not same_secret:
            detail_bits.append("its secret cannot be read back, so it cannot be proven to match this server")
        if not content_ok:
            detail_bits.append("content type is %r" % ((hook.get("config") or {}).get("content_type"),))
        if not push_only:
            detail_bits.append("events are %r, expected ['push']" % events)
        if not hook.get("active", True):
            detail_bits.append("the hook is inactive")
        detail = "; ".join(detail_bits)
        if self.o.update_webhook and self.prompt.confirm(
                "update webhook %s and set it to this server's secret" % hook_id, material=True,
                detail="this rotates the webhook secret in GitHub; the server side is already correct"):
            updated = api.update_hook(hook_id, hook_payload(self.payload_url, self.secret_value))
            if updated.ok:
                self.reporter.check("GitHub webhook", PASS, detail="webhook %s updated with the current secret" % hook_id)
                self._remember_webhook(hook_id, verified=self._verify_hook_delivery(api, hook_id))
            else:
                self.reporter.check("GitHub webhook", MANUAL,
                                    detail="GitHub returned %s %s" % (updated.status, updated.brief),
                                    fix="update the webhook secret in GitHub -> Settings -> Webhooks")
        else:
            self.reporter.check("GitHub webhook", MANUAL, detail="existing webhook %s: %s" % (hook_id, detail),
                                fix="rerun with --update-webhook to set its secret from this server, "
                                    "or check Recent Deliveries for a 200 'pong'")

    def _remember_webhook(self, hook_id: Any, verified: bool) -> None:
        self.record["webhook"] = {"id": hook_id, "url": self.payload_url,
                                  "secret_fingerprint": fingerprint(self.secret_value),
                                  "verified": bool(verified), "at": now_iso()}

    def _verify_hook_delivery(self, api: "GitHubAPI", hook_id: Any) -> bool:
        """Trigger a signed ping and read GitHub's delivery result (bounded waits)."""
        if hook_id is None or not self.may_write():
            return False
        ping = api.ping_hook(hook_id)
        if not ping.ok:
            self.reporter.warn("GitHub refused to send a test ping (%s %s)" % (ping.status, ping.brief))
            return False
        for wait in (3, 6, 10):
            self.sleep(wait)
            hook = api.get_hook(hook_id)
            if not hook.ok:
                continue
            try:
                payload = hook.json()
            except ValueError:
                continue
            last = payload.get("last_response") or {}
            code = last.get("code") or last.get("status")
            if code is not None and int(code) == 200:
                self.reporter.check("Webhook delivery", PASS,
                                    detail="GitHub's signed ping reached /deploy and returned 200 (hook %s)" % hook_id)
                return True
            if code is not None:
                self.reporter.check("Webhook delivery", FAIL,
                                    detail="GitHub's signed ping returned %s: %s" % (code, str(last.get("message") or "")[:80]),
                                    fix="check the PythonAnywhere error log and the secret in GitHub")
                return False
        self.reporter.check("Webhook delivery", MANUAL,
                            detail="GitHub has not reported the ping result yet",
                            fix="open GitHub -> Settings -> Webhooks -> Recent Deliveries")
        return False

    def _print_webhook_manual(self, reason: str) -> None:
        self.reporter.section("GitHub webhook steps (manual)")
        self.reporter.note("  Reason: %s" % reason)
        self.reporter.note("  1. Open https://github.com/%s/settings/hooks" % repo_owner_name(self.o.repo_url))
        self.reporter.note("  2. Add webhook (or edit the existing one):")
        self.reporter.note("       Payload URL:  %s" % self.payload_url)
        self.reporter.note("       Content type: application/json")
        self.reporter.note("       Secret:       the contents of %s" % self.secret_path)
        self.reporter.note("       Events:       Just the push event")
        self.reporter.note("       SSL verification: enabled (default)")
        self.reporter.note("  3. Add webhook, then confirm the signed ping shows 200 'pong' in Recent Deliveries.")
        self.reporter.note("  Note: nothing was created or changed in GitHub by this run.")
        if not self.o.show_secret:
            self.reporter.note("  Print the secret with: python3 %s --show-secret" % INSTALLER_NAME)

    # ===================================================================== #
    # Stage 7: staged validation
    # ===================================================================== #
    def stage_validation(self) -> None:
        self.reporter.stage("validation", "Staged validation")
        self._validate_files()
        self._validate_git()
        self._validate_environment_readonly()
        self._validate_secret_readonly()
        self._validate_wsgi_readonly()
        self._validate_signature_offline()
        self._validate_http()
        self._validate_webhook_state()
        self._validate_push_pull()

    def _validate_files(self) -> None:
        missing = [rel for rel in REQUIRED_PATHS if not (self.target / rel).exists()]
        if missing:
            self.reporter.check("Files installed", FAIL, detail="missing: %s" % ", ".join(missing),
                                fix="update the checkout, then rerun")
        else:
            self.reporter.check("Files installed", PASS, detail="%s contains the AMS application files" % self.target)

    def _validate_git(self) -> None:
        if not self.detected.is_git_repo:
            probe = self.runner.git("rev-parse", "--git-dir", cwd=self.target, timeout=20)
            if not probe.ok:
                self.reporter.check("Git checkout present", FAIL, detail="%s is not a Git checkout" % self.target,
                                    fix="run this installer without --check-only to bootstrap it")
                return
            self.detected = self.detect()
        origin_ok = normalize_repo_url(self.detected.origin_url) == normalize_repo_url(self.o.repo_url)
        self.reporter.check("Git origin", PASS if origin_ok else FAIL,
                            detail=redact_url(self.detected.origin_url or "<none>"),
                            fix="rerun with --set-origin after confirming the repository")
        branch_ok = self.detected.branch == self.o.branch
        self.reporter.check("Git branch", PASS if branch_ok else FAIL,
                            detail="%s (expected %s)" % (self.detected.branch or "detached", self.o.branch),
                            fix="rerun with --switch-branch if switching is intended")
        dirty = self.detected.dirty
        if not dirty:
            self.reporter.check("Git working tree clean", PASS, detail="the hook's 'git status --porcelain' guard is satisfied")
        else:
            self.reporter.check("Git working tree clean", MANUAL,
                                detail="%d change(s); the deploy hook returns 409 until this is clean" % len(dirty),
                                fix="resolve them deliberately (never `git reset --hard`); for the installer file use "
                                    "--restore-installer-file")
        head = self.runner.git("rev-parse", "--short", "HEAD", cwd=self.target, timeout=20)
        if head.ok:
            self.reporter.check("Checkout revision", INFO, required=False, detail=head.out.strip())

    def _validate_environment_readonly(self) -> None:
        if not self.venv_python.is_file():
            self.reporter.check("Virtualenv", FAIL, detail="%s is missing" % self.venv_python,
                                fix="rerun without --skip-deps to create it")
            return
        proc = self.runner.run([str(self.venv_python), "-c", "import sys;print('%d.%d.%d' % sys.version_info[:3])"], timeout=30)
        version = proc.out.strip() if proc.ok else ""
        self.reporter.check("Virtualenv", PASS if version else FAIL,
                            detail="%s (Python %s)" % (self.venv_python, version or "unknown"),
                            fix="recreate it with --recreate-venv")
        if not getattr(self, "_deps_validated", False):
            self._validate_environment()
            self._deps_validated = True

    def _validate_secret_readonly(self) -> None:
        path = self.secret_path
        if not path.is_file():
            self.reporter.check("Secret present", FAIL, detail="%s does not exist" % path,
                                fix="rerun without --skip-* options to create it")
            return
        value = ""
        try:
            value = read_text(path).strip()
        except OSError:
            pass
        if not is_secret_value_valid(value):
            self.reporter.check("Secret present", FAIL, detail="%s does not contain a usable secret" % path,
                                fix="rerun with --rotate-secret to replace it")
            return
        mode = file_mode(path)
        self.reporter.check("Secret present", PASS, detail="%s (%s, mode %03o)" % (path, fingerprint(value), mode & 0o777))
        if mode & 0o077:
            self.reporter.check("Secret permissions", FAIL, detail="mode %o is too open" % (mode & 0o777),
                                fix="chmod 600 %s (this installer fixes it on the next normal run)" % path)
        else:
            self.reporter.check("Secret permissions", PASS, detail="0600")

    def _validate_wsgi_readonly(self) -> None:
        path_text = self.detected.wsgi_file
        if not path_text:
            self.reporter.check("WSGI configured", FAIL, detail="no WSGI file path is known",
                                fix="pass --wsgi-file with the exact path from the Web tab")
            return
        path = Path(path_text)
        if not path.is_file():
            self.reporter.check("WSGI configured", MANUAL,
                                detail="%s does not exist yet" % path,
                                fix="create the web app in the Web tab first, then rerun (or use --create-wsgi-file)")
            return
        text = normalize_wsgi_source(read_text(path))
        blocks = text.count(MANAGED_BEGIN)
        if blocks != 1:
            self.reporter.check("WSGI configured", FAIL,
                                detail="found %d managed AMS blocks in %s (expected exactly 1)" % (blocks, path),
                                fix="rerun without --skip-wsgi to rewrite the block cleanly")
            return
        try:
            ast.parse(text)
        except SyntaxError as exc:
            self.reporter.check("WSGI configured", FAIL, detail="%s is not valid Python: %s" % (path, exc),
                                fix="restore the backup from %s and rerun" % (self.runtime_dir / "backups"))
            return
        checks = []
        if 'os.environ["AMS_WSGI_FILE"] = %r' % str(path) in text:
            checks.append("AMS_WSGI_FILE")
        else:
            self.reporter.check("WSGI configured", FAIL,
                                detail="AMS_WSGI_FILE in the block does not match %s" % path,
                                fix="rerun without --skip-wsgi")
            return
        if 'AMS_DEPLOY_BRANCH"] = %r' % self.o.branch in text:
            checks.append("AMS_DEPLOY_BRANCH")
        else:
            self.reporter.check("WSGI configured", FAIL, detail="AMS_DEPLOY_BRANCH is not %r in %s" % (self.o.branch, path),
                                fix="rerun without --skip-wsgi")
            return
        for marker in ("SQLITE_JOURNAL_MODE", "AMS_HTTPS", "from wsgi import application"):
            if marker in text:
                checks.append(marker)
        self.reporter.check("WSGI configured", PASS, detail="%s sets %s" % (path, ", ".join(checks)))

    @staticmethod
    def signature_self_check_snippet() -> str:
        return (
            "import hashlib, hmac, io, json, os, sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "import deploy_hook as hook\n"
            "secret = open(sys.argv[2], 'rb').read().strip()\n"
            "body = json.dumps({'zen': 'AMS setup signature self-check'}).encode()\n"
            "def call(env):\n"
            "    env = dict(env)\n"
            "    env['wsgi.input'] = io.BytesIO(body)\n"
            "    env['CONTENT_LENGTH'] = str(len(body))\n"
            "    statuses = []\n"
            "    out = hook.application(env, lambda s, h: statuses.append(s))\n"
            "    return statuses[0].split()[0], b''.join(out).decode('utf-8', 'replace').strip()\n"
            "good = 'sha256=' + hmac.new(secret, body, hashlib.sha256).hexdigest()\n"
            "ok_status, ok_body = call({'REQUEST_METHOD': 'POST', 'HTTP_X_GITHUB_EVENT': 'ping',"
            " 'HTTP_X_HUB_SIGNATURE_256': good})\n"
            "bad_status, _ = call({'REQUEST_METHOD': 'POST', 'HTTP_X_GITHUB_EVENT': 'ping',"
            " 'HTTP_X_HUB_SIGNATURE_256': 'sha256=' + '0' * 64})\n"
            "print('SIGCHECK %s %s %s' % (ok_status, bad_status, ok_body))\n"
        )

    def _validate_signature_offline(self) -> None:
        if not self.hook_path.is_file() or not self.secret_path.is_file():
            self.reporter.check("Signature (offline)", SKIP, required=False,
                                detail="hook or secret file is missing")
            return
        interpreter = str(self.venv_python) if self.venv_python.is_file() else (
            self.platform.interpreters[0][1] if self.platform.interpreters else sys.executable)
        result = self.runner.run(
            # -B plus the env var: the check must never leave __pycache__ in the checkout
            [interpreter, "-B", "-c", self.signature_self_check_snippet(), str(self.target), str(self.secret_path)],
            cwd=self.target, timeout=max(60, self.o.timeout),
            extra_env={"AMS_DEPLOY_SECRET_FILE": str(self.secret_path), "PYTHONDONTWRITEBYTECODE": "1"})
        match = re.search(r"SIGCHECK (\d+) (\d+) (.*)", result.out)
        if not result.ok or not match:
            self.reporter.check("Signature (offline)", FAIL,
                                detail=tail(result.combined, 5) or "the hook did not answer",
                                fix="check that deploy_hook.py imports cleanly with: %s -c 'import deploy_hook'" % interpreter)
            return
        good_status, bad_status, body = match.group(1), match.group(2), match.group(3).strip()
        if good_status == "200" and bad_status == "401" and body == "pong":
            self.reporter.check("Signature (offline)", PASS,
                                detail="the deployed hook accepts a valid signature and rejects a bad one")
        else:
            self.reporter.check("Signature (offline)", FAIL,
                                detail="valid=%s invalid=%s body=%r" % (good_status, bad_status, body),
                                fix="the hook code or the secret file is not the expected one")

    def _site_url(self, path: str = "") -> str:
        return "https://%s%s" % (self.o.domain, path)

    def _validate_http(self) -> None:
        if self.o.skip_http:
            self.reporter.check("Site reachable", SKIP, required=False, detail="--skip-http")
            return
        if self.o.dry_run:
            self.reporter.check("Site reachable", SKIP, required=False, detail="dry run: live checks are not sent")
            return
        root = self.http.request("GET", self._site_url("/"), retries=1, mutating=False)
        if root.status in (200, 301, 302, 303, 307, 308):
            self.reporter.check("Site reachable", PASS, detail="GET / -> %s" % root.status)
        elif root.status == 0:
            self.reporter.check("Site reachable", FAIL, detail="could not connect: %s" % (root.error or "unknown error"),
                                fix="check the domain name and the Web tab's error log")
            return
        else:
            self.reporter.check("Site reachable", FAIL, detail="GET / -> %s %s" % (root.status, root.brief),
                                fix="open the PythonAnywhere error log; the app may be failing to start")
            return
        health = self.http.request("GET", self._site_url(HEALTH_PATH), retries=1, mutating=False)
        if health.ok and "reachable" in health.body.lower():
            self.reporter.check("/deploy/health", PASS, detail="hook mounted (this alone does not prove deployment works)")
        else:
            self.reporter.check("/deploy/health", FAIL, detail="GET %s -> %s" % (HEALTH_PATH, health.brief),
                                fix="confirm the managed WSGI block is saved and click Reload")
            return
        self._validate_signature_live()

    def _validate_signature_live(self) -> None:
        if not self.secret_value or not is_secret_value_valid(self.secret_value):
            self.reporter.check("Signature (live)", SKIP, required=False, detail="no usable secret on the server")
            return
        body = PING_BODY
        signature = github_signature(self.secret_value, body)
        headers = {
            "Content-Type": "application/json",
            "X-GitHub-Event": "ping",
            "X-GitHub-Delivery": secrets.token_hex(16),
            "X-Hub-Signature-256": signature,
        }
        signed = self.http.request("POST", self._site_url(DEPLOY_PATH), data=body, headers=headers,
                                   retries=1, mutating=True, label="signed ping")
        if signed.ok and "pong" in signed.body.lower():
            self.reporter.check("Signature (live)", PASS,
                                detail="the live hook accepted a signed ping and answered 'pong' (no deployment was triggered)")
        elif signed.status == 503:
            self.reporter.check("Signature (live)", FAIL,
                                detail="the live hook says the deployment secret is not configured",
                                fix="check AMS_DEPLOY_SECRET_FILE and reload the web app")
        elif signed.status == 401:
            self.reporter.check("Signature (live)", FAIL,
                                detail="the live hook rejected the signature from this server's secret file",
                                fix="the running app is using a different secret; reload the web app and rerun")
        else:
            self.reporter.check("Signature (live)", FAIL, detail="POST /deploy -> %s" % signed.brief,
                                fix="check the PythonAnywhere error log")
            return
        unsigned = self.http.request("POST", self._site_url(DEPLOY_PATH), data=body,
                                     headers={"Content-Type": "application/json", "X-GitHub-Event": "ping",
                                              "X-GitHub-Delivery": secrets.token_hex(16)},
                                     retries=0, mutating=True, label="unsigned ping")
        if unsigned.status == 401:
            self.reporter.check("Signature enforcement", PASS, detail="an unsigned ping is rejected with 401")
        else:
            self.reporter.check("Signature enforcement", FAIL,
                                detail="an unsigned ping returned %s instead of 401" % unsigned.status,
                                fix="stop and investigate: the public endpoint must reject unsigned requests")

    def _validate_webhook_state(self) -> None:
        record = self.record.get("webhook") or self.detected.state.get("webhook") or {}
        if self.o.skip_webhook:
            self.reporter.check("Webhook configured", SKIP, required=False, detail="--skip-webhook")
            return
        if not record:
            self.reporter.check("Webhook configured", MANUAL,
                                detail="not configured or verified by this run",
                                fix="use the GitHub steps above (payload %s)" % (self.payload_url or self._site_url(DEPLOY_PATH)))
            return
        if record.get("verified"):
            self.reporter.check("Webhook configured", PASS,
                                detail="GitHub hook %s verified with a signed ping" % record.get("id"))
        else:
            self.reporter.check("Webhook configured", MANUAL,
                                detail="hook %s exists but delivery was not confirmed" % record.get("id"),
                                fix="open GitHub -> Settings -> Webhooks -> Recent Deliveries")

    def _latest_push_delivery(self) -> "tuple[bool, dict] | None":
        """Newest GitHub 'push' delivery for the AMS hook, when a token makes it visible."""
        if self.o.skip_webhook:
            return None
        record = self.record.get("webhook") or self.detected.state.get("webhook") or {}
        hook_id = record.get("id")
        if hook_id in (None, ""):
            return None
        token = self._existing_github_token()
        if not token:
            return None
        api = GitHubAPI(slug=repo_owner_name(self.o.repo_url), token=token, http=self.http, reporter=self.reporter)
        deliveries, response = api.list_deliveries(hook_id)
        if not response.ok:
            return None
        pushes = [item for item in deliveries if str(item.get("event")) == "push"]
        if not pushes:
            return None
        latest = pushes[0]
        code = latest.get("status_code")
        try:
            code_int = int(code)
        except (TypeError, ValueError):
            return None
        return (code_int == 200, latest)

    def _validate_push_pull(self) -> None:
        if self.o.dry_run:
            self.reporter.check("Push -> pull -> reload", SKIP, required=False, detail="dry run")
            return
        if not self.detected.is_git_repo:
            self.reporter.check("Push -> pull -> reload", MANUAL, detail="no Git checkout yet",
                                fix="bootstrap the checkout first, then rerun")
            return
        remote = self.runner.git("ls-remote", "origin", "refs/heads/%s" % self.o.branch, cwd=self.target, timeout=90,
                                 label="read origin/%s" % self.o.branch)
        if not remote.ok or not remote.out.strip():
            self.reporter.check("Push -> pull -> reload", FAIL,
                                detail="could not read origin/%s: %s" % (self.o.branch, tail(remote.combined, 3)),
                                fix="check network/credentials for %s" % repo_owner_name(self.o.repo_url))
            return
        remote_sha = remote.out.split()[0].strip()
        local = self.runner.git("rev-parse", "HEAD", cwd=self.target, timeout=20)
        local_sha = local.out.strip() if local.ok else ""
        if local_sha and local_sha == remote_sha:
            delivery = self._latest_push_delivery()
            if delivery is not None and delivery[0]:
                latest = delivery[1]
                when = str(latest.get("delivered_at") or "")[:19]
                self.reporter.check("Push -> pull -> reload", PASS,
                                    detail="GitHub delivery of a push at %s returned 200 and the checkout matches origin/%s (%s)"
                                           % (when or "some time ago", self.o.branch, remote_sha[:10]))
            else:
                detail = "server checkout already matches origin/%s (%s); no push delivery has been observed" \
                    % (self.o.branch, remote_sha[:10])
                if delivery is not None:
                    detail = "the newest GitHub push delivery returned %s" % delivery[1].get("status_code")
                self.reporter.check("Push -> pull -> reload", MANUAL, detail=detail,
                                    fix="push any commit to %s and check GitHub -> Recent Deliveries for "
                                        "'Deployed <sha>; WSGI reload requested'" % self.o.branch)
            if self.o.verify_push and not self.detected.dirty:
                confirm = self.prompt.confirm("verify the hook's pull path now (git fetch + merge --ff-only)",
                                              material=True)
                if confirm:
                    fetch = self._fetch_branch()
                    if fetch:
                        merge = self.runner.git("merge", "--ff-only", "origin/%s" % self.o.branch, cwd=self.target,
                                                mutating=True, timeout=180)
                        if merge.ok:
                            self.reporter.check("Pull path", PASS,
                                                detail="git fetch + merge --ff-only succeeds (still not a webhook delivery)")
                        else:
                            self.reporter.check("Pull path", FAIL, detail=merge.brief(),
                                                fix="resolve the checkout so the hook can fast-forward")
            return
        behind = self.runner.git("rev-list", "--count", "HEAD..origin/%s" % self.o.branch, cwd=self.target, timeout=30)
        ahead = self.runner.git("rev-list", "--count", "origin/%s..HEAD" % self.o.branch, cwd=self.target, timeout=30)
        behind_n = behind.out.strip() if behind.ok else "?"
        ahead_n = ahead.out.strip() if ahead.ok else "?"
        if ahead.ok and ahead_n not in ("", "0"):
            self.reporter.check("Push -> pull -> reload", FAIL,
                                detail="server checkout has %s local commit(s) not on origin/%s" % (ahead_n, self.o.branch),
                                fix="resolve the diverged checkout; the hook cannot fast-forward")
        else:
            self.reporter.check("Push -> pull -> reload", MANUAL,
                                detail="server checkout is %s commit(s) behind origin/%s" % (behind_n, self.o.branch),
                                fix="the hook fast-forwards on the next push to %s; verify in Recent Deliveries" % self.o.branch)

    # ===================================================================== #
    # Summary
    # ===================================================================== #
    def write_report(self) -> "Path | None":
        if not self.may_write():
            return None
        ensure_dir(self.logs_dir, 0o700)
        report = self.runtime_dir / ("report-%s.txt" % now_stamp())
        lines = ["AMS setup_Deploy.py %s report" % VERSION,
                 "generated: %s" % now_iso(),
                 "target: %s" % self.target,
                 "repository: %s (branch %s)" % (redact_url(self.o.repo_url), self.o.branch),
                 "domain: %s" % self.o.domain,
                 ""]
        for check in self.reporter.checks:
            lines.append("%-6s %-28s %s" % (check.status, check.name, check.detail))
            if check.fix and check.status in (FAIL, MANUAL):
                lines.append("       next: %s" % check.fix)
        text = self.redactor.redact("\n".join(lines)) + "\n"
        atomic_write(report, text, 0o600)
        return report

    def print_summary(self) -> int:
        counts = self.reporter.counts()
        blockers = self.reporter.blockers()
        manual = self.reporter.manual()
        self.reporter.section("Result")
        self.reporter.note("  checks: %d ok, %d todo, %d failed, %d skipped, %d warnings"
                           % (counts[PASS], counts[MANUAL], counts[FAIL], counts[SKIP], counts[WARN]))
        if blockers:
            self.reporter.note("")
            self.reporter.note("  Blocking problems:")
            for check in blockers:
                self.reporter.note("    - [%s] %s: %s" % (check.stage, check.name, check.detail))
                if check.fix:
                    self.reporter.note("      next: %s" % check.fix)
        if manual:
            self.reporter.note("")
            self.reporter.note("  Still needs you:")
            for check in manual:
                self.reporter.note("    - [%s] %s: %s" % (check.stage, check.name, check.detail))
                if check.fix:
                    self.reporter.note("      next: %s" % check.fix)
        report = self.write_report()
        if report is not None or self.reporter.log_path:
            self.reporter.note("")
            if self.reporter.log_path:
                self.reporter.note("  full log:    %s" % self.reporter.log_path)
            if report is not None:
                self.reporter.note("  report:      %s" % report)
        self.reporter.note("")
        if blockers:
            self.reporter.banner("SETUP INCOMPLETE — %d blocking problem(s) above" % len(blockers), "31")
            return 1
        if manual:
            self.reporter.banner("SETUP INCOMPLETE — %d manual step(s) remain" % len(manual), "33")
            return 2
        self.reporter.banner("SETUP COMPLETE — every required check passed", "32")
        return 0

    def show_secret_if_requested(self) -> None:
        if not self.o.show_secret:
            return
        if not self.secret_value:
            self.reporter.warn("--show-secret: no secret value is available in this run")
            return
        if not self.o.assume_yes and not self.prompt.confirm(
                "print the webhook secret to this terminal now",
                detail="anyone who sees it can post to /deploy; never paste it into chat, commits or logs",
                default=False):
            return
        self.reporter.raw("")
        self.reporter.raw("  webhook secret (%s) — copy it into GitHub, then clear the screen:"
                          % self.secret_path)
        self.reporter.raw("  " + self.secret_value)
        self.reporter.raw("")

    # ===================================================================== #
    # Orchestration
    # ===================================================================== #
    def run(self) -> int:
        self.reporter.section("AMS deployment setup %s" % VERSION)
        self.reporter.note("  host: %s   user: %s   python: %s   target: %s"
                           % (socket.gethostname(), self.platform.user,
                              "%s.%s" % sys.version_info[:2], self.target))
        if self.platform.is_pythonanywhere:
            self.reporter.note("  detected PythonAnywhere (%s); the Web tab Python version must match %s"
                               % (self.platform.pythonanywhere_domain or "env marker", self.venv_dir))
        self.reporter.log("start installer=%s pid=%s mode=%s" % (VERSION, os.getpid(),
                                                                 "dry-run" if self.o.dry_run else
                                                                 ("check-only" if self.o.check_only else "apply")))
        if not self.acquire_lock():
            return self.print_summary()
        try:
            self.detect()
            self.print_plan()
            if self.o.dry_run:
                self.stage_git()
                self.reporter.check("Dry run", INFO, required=False,
                                    detail="no file, Git, web app or webhook changes were made")
            elif self.o.check_only:
                self.reporter.note("")
                self.reporter.todo("check-only run: nothing is written; the validation stages below report the true state")
            else:
                proceed = self.prompt.confirm("run this plan now", default=True,
                                              noninteractive_default=True,
                                              detail="material changes are confirmed one by one; nothing is deleted")
                if not proceed:
                    self.reporter.check("Setup", MANUAL, detail="cancelled before any change",
                                        fix="rerun when ready")
                    return self.print_summary()
                self.stage_git()
                self.stage_python()
                self.stage_secret()
                self.stage_wsgi()
                created = self.stage_pythonanywhere()
                if created and not self.o.skip_wsgi:
                    self.reporter.note("")
                    self.reporter.note("  the web app was created in this run, so its WSGI file is written now")
                    self.stage_wsgi()
                if self.api is not None and self._web_app_ready:
                    self.reload_stage()
                self.stage_webhook()
                self.save_state()
            self.stage_validation()
            self.save_state()
        finally:
            self.release_lock()
        code = self.print_summary()
        self.show_secret_if_requested()
        self.reporter.log("exit code %d" % code)
        return code

    def reload_stage(self) -> None:
        if self.api is None:
            return
        self.reporter.stage("reload", "Web app reload")
        self._reload_web_app(self.api)

    # Backwards-compatible aliases used by tests and other callers.
    stage_reload = reload_stage


def github_signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def main(argv: "Sequence[str] | None" = None) -> int:
    try:
        options = parse_args(argv)
    except SystemExit as exc:  # argparse already printed the message
        return int(exc.code or 0)
    installer = Installer(options)
    try:
        return installer.run()
    except KeyboardInterrupt:
        installer.reporter.error("interrupted; rerun the installer — it re-checks the state and resumes")
        return 1


if __name__ == "__main__":
    sys.exit(main())
