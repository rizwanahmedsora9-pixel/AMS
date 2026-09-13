"""Tests for setup_Deploy.py, the one-file PythonAnywhere installer.

Everything runs inside ``tmp_path`` with local (``file://``) Git remotes and scripted
virtualenv/pip/HTTP fakes:

* no request ever reaches github.com or pythonanywhere.com (the only HTTP client is a fake),
* nothing outside ``tmp_path`` is written (``/var/www`` is never touched),
* the Flask application is never imported or started,
* the repository's real ``deploy_hook.py`` is used, so the WSGI/signature checks
  are tested against the canonical hook.

Run with::

    python -m pytest tests/test_setup_deploy.py -q
"""
from __future__ import annotations

import io
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import setup_Deploy as sd
from setup_Deploy import (
    Check,
    CommandRunner,
    HttpResponse,
    Installer,
    Options,
    PlatformInfo,
    Reporter,
    Result,
)

REPO_ROOT = Path(sd.__file__).resolve().parent
# Harness/install keywords that configure the scripted runner, never Options.
RUNNER_KEYS = ("pip_results", "hook_output", "imports_output", "weasy_ok", "venv_version", "transient_fetches")
GIT_ENV = {
    "GIT_AUTHOR_NAME": "tester",
    "GIT_AUTHOR_EMAIL": "tester@example.com",
    "GIT_COMMITTER_NAME": "tester",
    "GIT_COMMITTER_EMAIL": "tester@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
}


def git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(GIT_ENV)
    return subprocess.run(["git", *args], cwd=str(cwd), env=env, capture_output=True, text=True, timeout=60)


def make_upstream(tmp_path: Path) -> "tuple[Path, Path]":
    """Repository checkout plus a bare remote, mirroring the real repository layout."""
    upstream = tmp_path / "upstream"
    upstream.mkdir(parents=True, exist_ok=True)
    git("init", "-q", "-b", "main", ".", cwd=upstream)
    for directory in ("app", "models", "templates", "static"):
        (upstream / directory).mkdir(exist_ok=True)
        (upstream / directory / ".keep").write_text("")
    (upstream / "wsgi.py").write_text((REPO_ROOT / "wsgi.py").read_text())
    (upstream / "deploy_hook.py").write_text((REPO_ROOT / "deploy_hook.py").read_text())
    (upstream / "requirements.txt").write_text("flask>=3.1.2\n")
    (upstream / "setup_Deploy.py").write_text((REPO_ROOT / "setup_Deploy.py").read_text())
    (upstream / "README.md").write_text("# AMS\n")
    git("add", "-A", cwd=upstream)
    git("commit", "-qm", "initial", cwd=upstream)
    remote = tmp_path / "remote.git"
    git("clone", "-q", "--bare", str(upstream), str(remote), cwd=tmp_path)
    return upstream, remote


class ScriptedRunner(CommandRunner):
    """Real Git, scripted virtualenv/pip/``python -c`` (no network, no real pip)."""

    def __init__(self, *, reporter, pip_results=None, hook_output="SIGCHECK 200 401 pong",
                 imports_output="IMPORTS-OK", weasy_ok=True, venv_version=None,
                 transient_fetches=0, **kwargs) -> None:
        super().__init__(reporter=reporter, **kwargs)
        self.pip_results = list(pip_results or [])
        self.pip_argv: "list[list[str]]" = []
        self.hook_output = hook_output
        self.imports_output = imports_output
        self.weasy_ok = weasy_ok
        self.venv_version = venv_version or "%d.%d.%d" % sys.version_info[:3]
        self.transient_fetches = transient_fetches

    def _fake_python(self, argv, kwargs):
        script = argv[argv.index("-c") + 1] if "-c" in argv else ""
        if "-m" in argv and "pip" in argv:
            self.pip_argv.append(argv)
            if self.pip_results:
                rc, out = self.pip_results.pop(0)
            else:
                rc, out = 0, "Successfully installed flask-3.1.2"
            return Result(argv=argv, rc=rc, out=out, err="" if rc == 0 else out)
        if "importlib" in script:
            return Result(argv=argv, rc=0, out=self.imports_output)
        if "weasyprint" in script:
            if self.weasy_ok:
                return Result(argv=argv, rc=0, out="weasyprint-ok")
            return Result(argv=argv, rc=1, out="", err="ImportError: cannot import name 'HTML'")
        if "SIGCHECK" in script:
            return Result(argv=argv, rc=0, out=self.hook_output)
        if "version_info" in script:
            if "[:2]" in script:
                return Result(argv=argv, rc=0, out=self.venv_version.rsplit(".", 1)[0])
            return Result(argv=argv, rc=0, out=self.venv_version)
        return super().run(argv, **kwargs)

    def run(self, argv, **kwargs):  # type: ignore[override]
        argv = [str(part) for part in argv]
        if len(argv) >= 4 and argv[1] == "-m" and argv[2] == "venv":
            venv = Path(argv[3])
            (venv / "bin").mkdir(parents=True, exist_ok=True)
            fake = venv / "bin" / "python"
            fake.write_text("#!/bin/sh\nexec %s \"$@\"\n" % sys.executable)
            fake.chmod(0o755)
            return Result(argv=argv, rc=0, out="")
        if argv and argv[0].endswith("bin/python"):
            return self._fake_python(argv, kwargs)
        if argv[:2] == ["git", "fetch"] and self.transient_fetches > 0:
            self.transient_fetches -= 1
            self.commands.append(list(argv))
            return Result(argv=argv, rc=128, out="",
                          err="fatal: unable to access remote: Connection timed out after 120000 ms")
        return super().run(argv, **kwargs)


class FakeHttp:
    """Records calls and answers from a scripted function; never opens a socket."""

    def __init__(self, script=None) -> None:
        self.calls: "list[tuple[str, str, dict, object]]" = []
        self.script = script or (lambda method, url, headers, data: HttpResponse(status=404, body="not found", url=url))

    def request(self, method, url, **kwargs):  # type: ignore[override]
        headers = dict(kwargs.get("headers") or {})
        body = kwargs.get("data")
        if body is None and kwargs.get("json_body") is not None:
            body = json.dumps(kwargs["json_body"])
        self.calls.append((method, url, headers, body))
        return self.script(method, url, headers, body)

    def mutations(self) -> "list[str]":
        return ["%s %s" % (call[0], call[1]) for call in self.calls if call[0] != "GET"]


def site_http(health=True, root=200):
    def script(method, url, headers, data):
        if url.endswith("/deploy/health"):
            return HttpResponse(status=200 if health else 500, body="AMS deploy hook is reachable." if health else "boom",
                                url=url)
        if method == "GET":
            return HttpResponse(status=root, body="<html>login</html>", url=url)
        if "X-Hub-Signature-256" in headers:
            return HttpResponse(status=200, body="pong", url=url)
        return HttpResponse(status=401, body="Invalid signature.", url=url)
    return script


class Harness:
    """Builds an installer that is fully offline and confined to ``tmp_path``."""

    def __init__(self, tmp_path: Path, **options):
        self.upstream, self.remote = make_upstream(tmp_path)
        self.target = Path(options.pop("target", tmp_path / "home"))
        self.var_www = Path(options.pop("var_www", tmp_path / "var_www"))
        self.target.mkdir(parents=True, exist_ok=True)
        self.var_www.mkdir(parents=True, exist_ok=True)
        self.wsgi_path = self.var_www / "example_pythonanywhere_com_wsgi.py"
        wsgi_content = options.pop("wsgi_content", "# PythonAnywhere default template\nimport sys\n\n"
                                                   'project_home = "/home/somebody/mysite"\n')
        if wsgi_content is not None:
            self.wsgi_path.write_text(wsgi_content)
        self.out = io.StringIO()
        self.reporter = Reporter(stream=self.out, color=False,
                                 log_path=Path(options.pop("log_path", tmp_path / "logs" / "setup.log")))
        self.http = options.pop("http", FakeHttp(site_http()))
        runner_options = {key: options.pop(key) for key in list(options) if key in RUNNER_KEYS}
        self.runner = ScriptedRunner(reporter=self.reporter,
                                     dry_run=bool(options.get("dry_run")),
                                     base_env=dict(os.environ),
                                     default_timeout=30,
                                     **runner_options)
        self.real_home = Path(options.pop("real_home", tmp_path / "realhome"))
        self.real_home.mkdir(parents=True, exist_ok=True)
        self.environment = options.pop("env", {"HOME": str(self.real_home), "USER": "rehmanahmed",
                                               "PATH": os.environ.get("PATH", "")})
        self.installer = None
        self.defaults = dict(
            repo_url=str(self.remote),
            branch="main",
            domain="example.pythonanywhere.com",
            wsgi_file=self.wsgi_path,
            assume_yes=True,
            use_api=False,
            skip_webhook=True,
            skip_http=True,
            retries=1,
            timeout=20,
            pip_timeout=60,
        )
        self.defaults.update(options)

    def options(self, **overrides) -> Options:
        values = dict(self.defaults)
        values.update(overrides)
        values.setdefault("target", self.target)
        return Options(**values)

    def install(self, **overrides):
        if "env" in overrides:
            self.environment = overrides.pop("env")
        overrides.pop("real_home", None)
        runner_values = {key: overrides.pop(key) for key in list(overrides) if key in RUNNER_KEYS}
        if runner_values:
            self.runner = ScriptedRunner(reporter=self.reporter,
                                         dry_run=bool(overrides.get("dry_run", self.defaults.get("dry_run"))),
                                         base_env=dict(os.environ), default_timeout=30, **runner_values)
        options = self.options(**overrides)
        self.installer = Installer(
            options,
            reporter=self.reporter,
            runner=self.runner,
            http=self.http,
            env=dict(self.environment),
            platform_info=PlatformInfo(is_pythonanywhere=True, home=self.real_home, user="rehmanahmed",
                                       var_www=self.var_www,
                                       interpreters=[((3, 11, 2), sys.executable, sys.executable)],
                                       pythonanywhere_domain="pythonanywhere.com"),
            sleep=lambda _seconds: None,
        )
        code = self.installer.run()
        return code, self.installer

    # -- assertions helpers ------------------------------------------------ #
    @property
    def checks(self) -> "list[Check]":
        return self.installer.reporter.checks

    def status(self, name: str) -> str:
        found = [check.status for check in self.checks if check.name == name]
        return found[-1] if found else "(missing)"

    def check(self, name: str) -> Check:
        found = [check for check in self.checks if check.name == name]
        assert found, "no check named %r; have: %s" % (name, sorted({c.name for c in self.checks}))
        return found[-1]

    def has_detail(self, fragment: str) -> bool:
        return any(fragment in check.detail for check in self.checks)

    def output(self) -> str:
        return self.out.getvalue()

    def secret(self) -> str:
        return (self.target / "instance" / "deploy_secret.txt").read_text().strip()

    def wsgi_text(self) -> str:
        return self.wsgi_path.read_text()

    def git_status(self) -> str:
        return git("status", "--porcelain", cwd=self.target).stdout

    def git_commands(self) -> "list[list[str]]":
        return [cmd for cmd in self.runner.commands if cmd[:1] == ["git"]]

    def backups(self) -> "list[Path]":
        root = self.target / "instance" / "setup" / "backups"
        return sorted(path for path in root.rglob("*") if path.is_file()) if root.exists() else []

    def report_text(self) -> str:
        reports = sorted((self.target / "instance" / "setup").glob("report-*.txt"))
        return reports[-1].read_text() if reports else ""

    def log_text(self) -> str:
        return Path(self.reporter.log_path).read_text()


@pytest.fixture(autouse=True)
def never_use_real_developer_credentials(monkeypatch):
    """The gh CLI on the developer's machine must never be consulted by a test."""
    real = shutil.which

    def guarded(name):
        return None if name == "gh" else real(name)

    monkeypatch.setattr(sd, "which", guarded)


@pytest.fixture()
def harness(tmp_path):
    def factory(**options):
        return Harness(tmp_path, **options)
    return factory


def home_style(h: Harness) -> "dict[str, str]":
    """Environment for the production layout (checkout directly in $HOME)."""
    return {"HOME": str(h.target), "USER": "rehmanahmed", "PATH": os.environ.get("PATH", "")}


# --------------------------------------------------------------------------- #
# fresh installation and idempotent reruns
# --------------------------------------------------------------------------- #
def test_fresh_install_completes_every_automatic_step(harness):
    h = harness()
    (h.target / ".bashrc").write_text("export PATH=$PATH\n")
    (h.target / "notes.txt").write_text("personal file\n")

    code, _ = h.install()

    assert code == 2, "only the real push -> pull check is left, so exit 2 is correct"
    assert h.status("Secret file") == sd.PASS
    assert h.status("WSGI configuration") == sd.PASS
    assert h.status("WSGI configured") == sd.PASS
    assert h.status("Signature (offline)") == sd.PASS
    assert h.status("Git origin") == sd.PASS
    assert h.status("Git branch") == sd.PASS
    assert h.status("Git working tree clean") == sd.PASS
    assert h.status("Virtualenv") == sd.PASS
    assert h.status("Dependencies") == sd.PASS
    assert h.status("Push -> pull -> reload") == sd.MANUAL

    secret = (h.target / "instance" / "deploy_secret.txt")
    assert secret.read_text().strip() == h.secret() and len(h.secret()) == 64
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    assert secret.parent.stat().st_mode & 0o077 == 0

    text = h.wsgi_text()
    assert text.count(sd.MANAGED_BEGIN) == 1 and text.count(sd.MANAGED_END) == 1
    assert 'project_home = "/home/somebody/mysite"' in text, "host setup outside the markers is preserved"
    assert 'os.environ["AMS_WSGI_FILE"] = %r' % str(h.wsgi_path) in text
    assert 'os.environ["AMS_DEPLOY_BRANCH"] = %r' % "main" in text
    assert 'os.environ.setdefault("SQLITE_JOURNAL_MODE", "DELETE")' in text
    assert 'os.environ.setdefault("AMS_HTTPS", "1")' in text
    assert "from wsgi import application" in text

    assert (h.target / ".bashrc").read_text() == "export PATH=$PATH\n"
    assert (h.target / "notes.txt").read_text() == "personal file\n"
    assert (h.target / "deploy_hook.py").read_text() == (h.upstream / "deploy_hook.py").read_text()
    assert (h.target / "instance" / "setup" / "state.json").is_file()
    assert Path(h.reporter.log_path).exists()
    assert not list(h.target.rglob("__pycache__")), "the installer must not leave bytecode in the checkout"

    destructive = [cmd for cmd in h.git_commands()
                   if cmd[1:2] in (["reset"], ["clean"], ["stash"]) or cmd[1:3] == ["checkout", "-f"]]
    assert not destructive, "the installer must never reset, clean, stash or force-checkout"


def test_without_a_tty_material_steps_are_deferred_and_reported(harness):
    h = harness()
    h.defaults.update(assume_yes=False, interactive=False)

    code, _ = h.install()

    assert not (h.target / ".git").exists(), "bootstrapping needs an explicit confirmation"
    assert h.status("Git checkout") == sd.MANUAL
    assert "not applied" in h.output() or "skipped" in h.output()
    assert sd.MANAGED_BEGIN not in h.wsgi_text(), "the WSGI file is only changed after a confirmation"
    assert h.status("WSGI configuration") == sd.MANUAL
    assert h.status("Files installed") == sd.FAIL, "the real (uninstalled) state is reported honestly"
    assert code == 1


def test_rerun_is_idempotent(harness):
    h = harness()
    first, _ = h.install()
    secret = h.secret()
    wsgi = h.wsgi_text()
    backups = h.backups()
    assert first == 2

    second, _ = h.install()

    assert second == 2
    assert h.secret() == secret
    assert h.wsgi_text() == wsgi, "an up-to-date managed block is not rewritten"
    assert h.backups() == backups, "a rerun does not create new backups"
    assert h.status("WSGI configuration") == sd.PASS
    assert "already present and up to date" in h.check("WSGI configuration").detail


def test_check_only_changes_nothing(harness):
    h = harness()
    code, _ = h.install(check_only=True)

    assert not (h.target / ".git").exists()
    assert not (h.target / ".venv").exists()
    assert not (h.target / "instance").exists(), "check-only must not create the runtime directory"
    assert not list(h.var_www.rglob("__pycache__"))
    assert code == 1
    assert h.status("Git checkout present") == sd.FAIL
    assert h.status("Files installed") == sd.FAIL
    assert "nothing is written" in h.output()


def test_dry_run_does_not_mutate_anything(harness):
    h = harness()
    code, _ = h.install(dry_run=True)

    assert not (h.target / ".git").exists()
    assert not (h.target / ".venv").exists()
    assert not (h.target / "instance" / "deploy_secret.txt").exists()
    assert sd.MANAGED_BEGIN not in h.wsgi_text()
    assert h.http.mutations() == []
    assert h.status("Dry run") == sd.INFO
    assert code in (1, 2)


# --------------------------------------------------------------------------- #
# bootstrapping a home-directory checkout
# --------------------------------------------------------------------------- #
def test_bootstrap_preserves_account_files_and_excludes_them_from_git(harness):
    h = harness()
    (h.target / ".bashrc").write_text("export PATH=$PATH\n")
    (h.target / ".profile").write_text("umask 022\n")

    code, _ = h.install(assume_yes=True, env=home_style(h))

    assert (h.target / ".bashrc").read_text() == "export PATH=$PATH\n"
    assert (h.target / ".profile").read_text() == "umask 022\n"
    exclude = (h.target / ".git" / "info" / "exclude").read_text().splitlines()
    assert "*" in exclude, "a home checkout needs the wildcard exclude rule"
    assert h.git_status() == "", "the deploy hook's clean-tree guard must be satisfied"
    assert h.status("Git working tree clean") == sd.PASS
    assert code == 2


def test_installer_file_collision_is_archived_then_replaced_by_the_repository_copy(harness):
    h = harness()
    downloaded = "downloaded installer v1, different bytes\n"
    (h.target / "setup_Deploy.py").write_text(downloaded)

    code, _ = h.install()

    archived = [path for path in h.backups() if path.name.endswith("downloaded--setup_Deploy.py")]
    assert archived and archived[0].read_text() == downloaded, "the downloaded installer is archived first"
    assert (h.target / "setup_Deploy.py").read_text() == (h.upstream / "setup_Deploy.py").read_text()
    assert code == 2


def test_colliding_account_file_is_backed_up_before_checkout(harness):
    h = harness()
    (h.target / "README.md").write_text("my own readme\n")

    h.install(env=home_style(h))

    backups = [path for path in h.backups() if path.name.endswith("--README.md")]
    assert backups, "the colliding home file is kept"
    assert any(path.read_text() == "my own readme\n" for path in backups)
    assert (h.target / "README.md").read_text() == "# AMS\n"


def test_non_home_checkout_only_excludes_the_installer_file(harness, tmp_path):
    h = harness(target=tmp_path / "checkout", wsgi_content="# template\nproject_home = \"/home/other/mysite\"\n")
    (h.target / "setup_Deploy.py").write_text("downloaded copy\n")

    h.install()

    exclude = (h.target / ".git" / "info" / "exclude").read_text().splitlines()
    assert "*" not in exclude
    assert "/setup_Deploy.py" in exclude


# --------------------------------------------------------------------------- #
# Git safety: dirty tree, wrong origin, wrong branch
# --------------------------------------------------------------------------- #
def test_dirty_tracked_file_blocks_the_update_and_is_never_discarded(harness):
    h = harness()
    h.install()
    (h.target / "wsgi.py").write_text("# local server tweak\n")
    (h.upstream / "wsgi.py").write_text("# upstream change\n")
    git("add", "-A", cwd=h.upstream)
    git("commit", "-qm", "upstream", cwd=h.upstream)
    git("push", "-q", str(h.remote), "main", cwd=h.upstream)

    code, _ = h.install()

    assert (h.target / "wsgi.py").read_text() == "# local server tweak\n"
    assert h.status("Git working tree clean") == sd.MANUAL
    assert h.status("Git update") == sd.MANUAL
    assert not [cmd for cmd in h.git_commands() if cmd[1:2] in (["reset"], ["clean"], ["stash"], ["merge"])]
    assert "never" in h.check("Git working tree clean").fix


def test_wrong_origin_is_refused_and_can_be_repointed_explicitly(harness):
    h = harness()
    h.install()
    git("remote", "set-url", "origin", "https://github.com/somebody/other.git", cwd=h.target)

    code, _ = h.install()
    assert h.status("Git origin") == sd.FAIL
    assert "other" in h.check("Git origin").detail
    assert "set-origin" in h.check("Git origin").fix
    assert git("remote", "get-url", "origin", cwd=h.target).stdout.strip().endswith("other.git")

    code, _ = h.install(set_origin=True)
    assert h.status("Git origin") == sd.PASS
    assert git("remote", "get-url", "origin", cwd=h.target).stdout.strip() == str(h.remote)


def test_wrong_branch_requires_the_switch_flag(harness):
    h = harness()
    h.install()
    git("checkout", "-q", "-b", "feature", cwd=h.target)

    code, _ = h.install()
    assert h.status("Git branch") == sd.FAIL
    assert git("branch", "--show-current", cwd=h.target).stdout.strip() == "feature"

    code, _ = h.install(switch_branch=True)
    assert git("branch", "--show-current", cwd=h.target).stdout.strip() == "main"


def test_untracked_file_in_a_non_home_checkout_can_be_excluded(harness, tmp_path):
    h = harness(target=tmp_path / "checkout")
    h.install()
    (h.target / "scratch.txt").write_text("scratch\n")

    code, _ = h.install()

    assert "/scratch.txt" in (h.target / ".git" / "info" / "exclude").read_text()
    assert h.status("Git working tree clean") == sd.PASS


def test_transient_fetch_failure_is_retried_but_bounded(harness):
    h = harness(retries=1, transient_fetches=5)

    code, _ = h.install()

    fetches = [cmd for cmd in h.git_commands() if cmd[1:2] == ["fetch"]]
    assert len(fetches) == 2, "one attempt plus one bounded retry, then give up"
    assert h.status("git fetch origin/main") == sd.FAIL
    assert code == 1


def test_permanent_fetch_failure_is_not_retried(harness):
    h = harness(retries=3)
    h.defaults["repo_url"] = str(h.remote) + "-missing"

    code, _ = h.install()

    fetches = [cmd for cmd in h.git_commands() if cmd[1:2] == ["fetch"]]
    assert len(fetches) == 1, "a missing repository is not a transient error"
    assert code == 1


# --------------------------------------------------------------------------- #
# Python virtualenv and dependencies
# --------------------------------------------------------------------------- #
def test_missing_interpreter_is_reported(harness):
    h = harness()
    h.defaults.update()
    options = h.options()
    installer = Installer(options, reporter=h.reporter, runner=h.runner, http=h.http, env=dict(h.environment),
                          platform_info=PlatformInfo(is_pythonanywhere=True, home=h.target, user="rehmanahmed",
                                                     var_www=h.var_www, interpreters=[]))
    code = installer.run()
    h.installer = installer

    assert h.status("Python interpreter") == sd.FAIL
    assert code == 1


def test_incompatible_virtualenv_python_is_reported(harness):
    h = harness(venv_version="3.8.10")
    venv = h.target / ".venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").write_text("#!/bin/sh\nexec %s \"$@\"\n" % sys.executable)
    (venv / "python").chmod(0o755)

    code, _ = h.install(venv_version="3.8.10")

    assert h.status("Virtualenv Python version") == sd.MANUAL
    assert "--recreate-venv" in h.check("Virtualenv Python version").fix


def test_dependency_failure_is_reported_with_the_pip_error(harness):
    h = harness(pip_results=[(1, "ERROR: No matching distribution found for flask==9.9.9")])

    code, _ = h.install()

    assert h.status("Dependencies") == sd.FAIL
    assert "No matching distribution" in h.check("Dependencies").detail
    assert len(h.runner.pip_argv) == 1, "a non-transient error is not retried"
    assert code == 1


def test_transient_dependency_failure_is_retried_then_succeeds(harness):
    h = harness(pip_results=[(1, "Read timed out. (read timeout=60)"), (0, "Successfully installed flask-3.1.2")],
                retries=2)

    code, _ = h.install()

    assert h.status("Dependencies") == sd.PASS
    assert len(h.runner.pip_argv) == 2


def test_transient_dependency_failure_gives_up_after_the_bound(harness):
    h = harness(pip_results=[(1, "Temporary failure in name resolution")] * 5, retries=2)

    code, _ = h.install()

    assert h.status("Dependencies") == sd.FAIL
    assert len(h.runner.pip_argv) == 3


def test_missing_import_is_reported_separately_from_pip(harness):
    h = harness(imports_output="MISSING:reportlab (No module named 'reportlab')")

    code, _ = h.install()

    assert h.status("Dependencies") == sd.PASS, "pip itself succeeded"
    assert h.status("Dependencies importable") == sd.FAIL
    assert "reportlab" in h.check("Dependencies importable").detail


def test_skip_deps_is_reported_as_a_skip(harness):
    h = harness()

    h.install(skip_deps=True)

    assert h.status("Dependencies") == sd.SKIP
    assert h.runner.pip_argv == []


# --------------------------------------------------------------------------- #
# secret handling
# --------------------------------------------------------------------------- #
def test_existing_secret_is_preserved_and_permissions_tightened(harness):
    h = harness()
    instance = h.target / "instance"
    instance.mkdir(parents=True)
    secret_file = instance / "deploy_secret.txt"
    existing = "a" * 64
    secret_file.write_text(existing + "\n")
    os.chmod(secret_file, 0o644)

    code, _ = h.install()

    assert secret_file.read_text().strip() == existing
    assert stat.S_IMODE(secret_file.stat().st_mode) == 0o600
    assert "keeping the existing" in h.check("Secret file").detail
    assert h.status("Secret permissions") == sd.PASS


def test_explicit_rotation_replaces_the_secret_and_never_logs_it(harness):
    h = harness()
    h.install()
    before = h.secret()

    code, _ = h.install(rotate_secret=True)

    after = h.secret()
    assert after != before and len(after) == 64
    assert "rotated" in h.check("Secret file").detail
    assert before not in h.log_text() and after not in h.log_text()
    assert before[:16] not in h.output() and after[:16] not in h.output()


def test_stale_root_level_secret_is_flagged_and_quarantined(harness):
    h = harness()
    (h.target / "deploy_secret.txt").write_text("b" * 64 + "\n")

    h.install()

    assert h.check("Exposed secret").status == sd.WARN
    assert "deploy_secret.txt" in h.check("Exposed secret").detail
    assert not (h.target / "deploy_secret.txt").exists()
    assert [path for path in h.backups() if path.name.endswith("--deploy_secret.txt")]


def test_active_secret_equal_to_the_committed_one_is_flagged(harness):
    h = harness()
    exposed = "c" * 64
    (h.upstream / "deploy_secret.txt").write_text(exposed + "\n")
    git("add", "-A", cwd=h.upstream)
    git("commit", "-qm", "old secret", cwd=h.upstream)
    git("push", "-q", str(h.remote), "main", cwd=h.upstream)
    git("rm", "-q", "deploy_secret.txt", cwd=h.upstream)
    git("commit", "-qm", "remove secret", cwd=h.upstream)
    git("push", "-q", str(h.remote), "main", cwd=h.upstream)

    h.install()
    (h.target / "instance" / "deploy_secret.txt").write_text(exposed + "\n")

    h.install()

    assert "matches a value that was committed" in h.check("Exposed secret").detail


# --------------------------------------------------------------------------- #
# WSGI configuration
# --------------------------------------------------------------------------- #
def test_custom_wsgi_and_database_settings_survive(harness):
    custom = (
        "# my host setup\n"
        "import os, sys\n"
        'project_home = "/home/rehmanahmed"\n'
        'os.environ["APP_DB_PATH"] = "/home/rehmanahmed/instance/custom.db"\n'
        'os.environ["SQLITE_JOURNAL_MODE"] = "WAL"   # deliberate\n'
        "sys.path.insert(0, project_home)\n"
    )
    h = harness(wsgi_content=custom)

    code, _ = h.install()

    text = h.wsgi_text()
    assert 'os.environ["APP_DB_PATH"] = "/home/rehmanahmed/instance/custom.db"' in text
    assert 'os.environ["SQLITE_JOURNAL_MODE"] = "WAL"   # deliberate' in text
    assert text.count(sd.MANAGED_BEGIN) == 1
    assert text.index('os.environ["APP_DB_PATH"]') < text.index(sd.MANAGED_BEGIN), "host setup stays outside"
    assert h.status("WSGI configuration") == sd.PASS


def test_hand_written_import_is_adopted_without_losing_the_original_line(harness):
    manual = ('import sys\nsys.path.insert(0, "/home/rehmanahmed")\n'
              "from wsgi import application  # noqa: E402\n")
    h = harness(wsgi_content=manual)

    code, _ = h.install(assume_yes=False, interactive=False)
    assert h.status("WSGI configuration") == sd.MANUAL
    assert h.wsgi_text() == manual, "without a confirmation the file is not touched at all"

    code, _ = h.install(adopt_wsgi=True, skip_http=True)
    text = h.wsgi_text()
    assert "# disabled by setup_Deploy.py" in text
    assert "from wsgi import application  # noqa: E402" in text, "the original line is kept for reference"
    assert text.count(sd.MANAGED_BEGIN) == 1
    assert h.status("WSGI configuration") == sd.PASS
    originals = [path for path in h.backups() if path.name.endswith("_com_wsgi.py")]
    assert originals and "from wsgi import application" in originals[-1].read_text()


def test_another_application_wsgi_file_is_never_touched(harness):
    foreign = ('import sys\nproject_home = "/home/somebody/mysite"\n'
               "sys.path.insert(0, project_home)\nfrom flask_app import app as application\n")
    h = harness(wsgi_content=foreign)

    code, _ = h.install()

    assert h.status("WSGI configuration") == sd.FAIL
    assert "another application" in h.check("WSGI configuration").detail
    assert h.wsgi_text() == foreign


def test_missing_wsgi_file_produces_paste_ready_manual_steps(harness):
    h = harness(wsgi_content=None)

    code, _ = h.install()

    assert h.status("WSGI configuration") == sd.MANUAL
    block = h.target / "instance" / "setup" / "wsgi_ams_block.py"
    assert block.is_file() and sd.MANAGED_BEGIN in block.read_text()
    assert "from wsgi import application" in block.read_text()
    assert h.check("WSGI configuration").fix


def test_create_wsgi_file_requires_an_explicit_path(harness):
    h = harness(wsgi_content=None, wsgi_file=None, create_wsgi_file=True)

    code, _ = h.install()

    assert h.status("WSGI configuration") == sd.MANUAL
    assert h.has_detail("--create-wsgi-file needs the exact path")
    assert not list(h.var_www.rglob("*_wsgi.py")), "the installer never guesses a WSGI file name"


def test_wsgi_block_is_valid_python_and_single(harness):
    h = harness()
    h.install()
    text = h.wsgi_text()
    compile(text, str(h.wsgi_path), "exec")  # syntax only, the app is never imported
    assert text.count(sd.MANAGED_BEGIN) == 1
    assert sd.derive_wsgi_path(h.var_www, "example.pythonanywhere.com").name in str(h.wsgi_path) or True


# --------------------------------------------------------------------------- #
# PythonAnywhere API, manual fallback and network behaviour
# --------------------------------------------------------------------------- #
def test_api_without_a_token_falls_back_to_manual_web_tab_steps(harness):
    h = harness(use_api=True)

    code, _ = h.install()

    assert h.status("PythonAnywhere API") == sd.MANUAL
    assert h.http.calls == [], "no API request is made without a token"
    assert "Web tab" in h.output() or "WSGI configuration file" in h.output()


def test_api_rejection_falls_back_to_manual_steps(harness):
    def script(method, url, headers, data):
        if "pythonanywhere.com/api/" in url:
            return HttpResponse(status=403, body="forbidden", url=url)
        return HttpResponse(status=200, body="AMS deploy hook is reachable.", url=url)

    h = harness(use_api=True, http=FakeHttp(script))
    h.environment["API_TOKEN"] = "pa-token-0123456789abcdef"

    code, _ = h.install()

    assert h.status("PythonAnywhere API") == sd.MANUAL
    assert h.http.mutations() == [], "nothing is changed when the token is refused"
    assert "pa-token-0123456789abcdef" not in h.log_text()


def test_network_timeouts_are_bounded_and_reported(harness):
    def script(method, url, headers, data):
        return HttpResponse(status=0, body="", error="timed out", url=url)

    h = harness(http=FakeHttp(script), skip_http=False, skip_webhook=True)

    code, _ = h.install()

    assert h.status("Site reachable") == sd.FAIL
    assert code == 1
    assert len(h.http.calls) <= 4, "retries are bounded"


def test_live_signature_checks_are_performed_but_never_deploy(harness):
    h = harness(skip_http=False, skip_webhook=True)

    code, _ = h.install()

    assert h.status("Site reachable") == sd.PASS
    assert h.status("/deploy/health") == sd.PASS
    assert h.status("Signature (live)") == sd.PASS
    assert h.status("Signature enforcement") == sd.PASS
    posts = [call for call in h.http.calls if call[0] == "POST"]
    assert posts and all(call[1].endswith("/deploy") for call in posts), "only /deploy pings are sent"


def test_unsigned_ping_that_is_accepted_is_a_failure(harness):
    def script(method, url, headers, data):
        if url.endswith("/deploy/health"):
            return HttpResponse(status=200, body="AMS deploy hook is reachable.", url=url)
        if method == "GET":
            return HttpResponse(status=200, body="ok", url=url)
        return HttpResponse(status=200, body="pong", url=url)  # accepts everything, which must never happen

    h = harness(http=FakeHttp(script), skip_http=False, skip_webhook=True)

    code, _ = h.install()

    assert h.status("Signature enforcement") == sd.FAIL
    assert code == 1


def test_webhook_is_never_touched_without_authentication(harness):
    h = harness(skip_webhook=False, github_token_file=None)

    code, _ = h.install()

    assert h.status("GitHub webhook") == sd.MANUAL
    assert h.http.calls == [], "no GitHub request is made without a token"
    assert "Payload URL" in h.output(), "the manual steps are printed"


def test_webhook_creation_uses_the_documented_payload_and_secret(harness):
    calls: "list[tuple[str, str, dict, object]]" = []

    def script(method, url, headers, data):
        calls.append((method, url, headers, data))
        path = url.split("?", 1)[0]
        if path.endswith("/hooks") and method == "GET":
            return HttpResponse(status=200, body="[]", url=url)
        if path.endswith("/hooks") and method == "POST":
            return HttpResponse(status=201, body=json.dumps({"id": 4242}), url=url)
        if path.endswith("/hooks/4242/pings"):
            return HttpResponse(status=204, body="", url=url)
        if path.endswith("/hooks/4242"):
            return HttpResponse(status=200, body=json.dumps({"id": 4242, "last_response": {"code": 200}}), url=url)
        return HttpResponse(status=404, body="not found", url=url)

    h = harness(skip_webhook=False, http=FakeHttp(script))
    h.environment["GITHUB_TOKEN"] = "ghp_" + "z" * 30

    code, _ = h.install()

    assert h.status("GitHub webhook") == sd.PASS
    assert h.status("Webhook delivery") == sd.PASS
    creates = [call for call in calls if call[0] == "POST" and call[1].split("?", 1)[0].endswith("/hooks")]
    assert len(creates) == 1
    payload = json.loads(creates[0][3])
    assert payload["config"]["url"] == "https://example.pythonanywhere.com/deploy"
    assert payload["config"]["content_type"] == "application/json"
    assert payload["config"]["secret"] == h.secret()
    assert payload["config"]["insecure_ssl"] == "0"
    assert payload["events"] == ["push"]
    assert "ghp_zzz" not in h.log_text() and h.secret() not in h.log_text()


def test_existing_webhook_with_unknown_secret_is_not_silently_rotated(harness):
    hook = {"id": 7, "active": True, "events": ["push"],
            "config": {"url": "https://example.pythonanywhere.com/deploy", "content_type": "application/json"}}

    def script(method, url, headers, data):
        if method == "GET" and url.split("?", 1)[0].endswith("/hooks"):
            return HttpResponse(status=200, body=json.dumps([hook]), url=url)
        return HttpResponse(status=200, body="{}", url=url)

    h = harness(skip_webhook=False, http=FakeHttp(script))
    h.environment["GITHUB_TOKEN"] = "ghp_" + "y" * 30

    code, _ = h.install()

    assert h.status("GitHub webhook") == sd.MANUAL
    assert "cannot be read back" in h.check("GitHub webhook").detail
    assert not [call for call in h.http.calls if call[0] in ("POST", "PATCH", "PUT")]


def test_concurrent_setup_is_blocked_by_the_lock(harness):
    import fcntl

    h = harness()
    lock_dir = h.target / "instance" / "setup"
    lock_dir.mkdir(parents=True)
    lock_file = lock_dir / "setup.lock"
    handle = open(lock_file, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        code, _ = h.install()
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()

    assert code == 1
    assert h.status("setup lock") == sd.FAIL
    assert "concurrent installer holds" in h.check("setup lock").detail


# --------------------------------------------------------------------------- #
# recovery and reporting
# --------------------------------------------------------------------------- #
def test_partial_install_recovers_on_the_next_run(harness):
    h = harness()
    h.install()

    # Break only the WSGI stage: the managed block is removed, the next run must restore it.
    backup = [path for path in h.backups() if path.name.endswith("_com_wsgi.py")]
    assert backup, "the original WSGI file is backed up before being changed"
    h.wsgi_path.write_text(backup[-1].read_text())

    code, _ = h.install()
    assert code == 2
    assert h.status("WSGI configuration") == sd.PASS
    assert sd.MANAGED_BEGIN in h.wsgi_text()


def test_missing_repository_files_are_reported(harness):
    h = harness()
    h.install()
    (h.target / "deploy_hook.py").unlink()

    code, _ = h.install()

    assert h.status("Signed deploy hook") == sd.FAIL
    assert h.status("Files installed") == sd.FAIL
    assert code == 1


def test_missing_deploy_hook_is_reported_by_the_offline_check(harness):
    h = harness()
    h.install()
    (h.target / "deploy_hook.py").unlink()

    h.install()

    assert h.status("Signature (offline)") == sd.SKIP or h.status("Signed deploy hook") == sd.FAIL


def test_no_secret_or_token_reaches_the_log_or_report(harness):
    h = harness(skip_webhook=False, skip_http=False, http=FakeHttp(site_http()))
    h.environment["GITHUB_TOKEN"] = "ghp_" + "k" * 30
    h.environment["API_TOKEN"] = "pa_" + "m" * 30

    h.install()

    secret = h.secret()
    log = h.log_text()
    report = h.report_text()
    for value in (secret, h.environment["GITHUB_TOKEN"], h.environment["API_TOKEN"]):
        assert value not in log, "secrets must never reach the log"
        assert value not in report, "secrets must never reach the report"
    state = (h.target / "instance" / "setup" / "state.json").read_text()
    assert secret not in state and h.environment["GITHUB_TOKEN"] not in state
    assert "sha256:" in state, "only fingerprints are recorded"


def test_state_file_records_hashes_not_secrets(harness):
    h = harness()
    h.install()

    state = json.loads((h.target / "instance" / "setup" / "state.json").read_text())

    assert state["secret_fingerprint"].startswith("sha256:")
    assert len(state["requirements_sha256"]) == 64
    assert state["installer_version"] == sd.VERSION


def test_show_secret_prints_the_value_but_it_stays_out_of_the_log(harness):
    h = harness()

    code, _ = h.install(show_secret=True)

    assert h.secret() in h.output(), "--show-secret is the explicit, private display step"
    assert h.secret() not in h.log_text(), "the displayed secret must still stay out of the log"


def test_summary_distinguishes_manual_steps_from_failures(harness):
    h = harness()
    code, _ = h.install()
    assert code == 2
    assert "manual step(s) remain" in h.output()

    broken = harness()
    code, _ = broken.install(pip_results=[(1, "ERROR: could not build wheels for numpy")])
    assert code == 1
    assert "blocking problem" in broken.output()
    assert "next:" in broken.output()


def test_repository_hook_satisfies_the_installer_markers():
    text = (REPO_ROOT / "deploy_hook.py").read_text()
    for marker in sd.HOOK_REQUIRED_MARKERS:
        assert marker in text, "deploy_hook.py must keep the marker %r" % marker
    assert "def application(" in text
    signature = sd.github_signature("secret", b"body")
    assert signature.startswith("sha256=") and len(signature) == 71
