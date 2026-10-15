"""Standalone deployment tests; no app/database startup or real pulls."""
import hashlib
import hmac
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import deploy_hook as hook


class DeployHookTests(unittest.TestCase):
    def setUp(self):
        # Keep deploy status out of the real instance/ directory.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self._status_patch = patch.object(
            hook, "STATUS_FILE", Path(tmp.name) / "deploy_status.json")
        self._status_patch.start()
        self.addCleanup(self._status_patch.stop)
        self.addCleanup(hook.join_deploy, 5)

    def request(self, body=None, event="push", method="POST", signature=None, **extra):
        if body is None:
            body = json.dumps({"ref": f"refs/heads/{hook.BRANCH}"}).encode()
        env = {"REQUEST_METHOD": method, "CONTENT_LENGTH": str(len(body)),
               "wsgi.input": io.BytesIO(body), "HTTP_X_GITHUB_EVENT": event,
               "HTTP_X_GITHUB_DELIVERY": "test-delivery",
               "HTTP_X_HUB_SIGNATURE_256": signature if signature is not None else
               "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()}
        env.update(extra)
        statuses = []
        with patch.object(hook, "_secret", return_value="test-secret"):
            result = hook.application(env, lambda status, headers: statuses.append(status))
        return statuses[0], b"".join(result)

    def test_signed_push_queues_deploy(self):
        with patch.object(hook, "_deploy", return_value=("200 OK", "deployed")) as deploy:
            status, body = self.request()
            self.assertEqual(status, "200 OK")
            self.assertIn(b"queued", body.lower())
            self.assertTrue(hook.join_deploy(timeout=5))
            deploy.assert_called_once()

    def test_large_push_payload_still_delivers(self):
        """The old 2 MB cap answered 413 'Payload too large.' before the ref was
        inspected, so big pushes (bulk imports, patch stacks) failed delivery
        even when they would have been ignored. A multi-megabyte signed push
        must now be acknowledged like any other."""
        big = b'{"ref":"refs/heads/arena/some-branch","pad":"' + b"a" * (3 * 1024 * 1024) + b'"}'
        status, body = self.request(body=big)
        self.assertEqual(status, "200 OK")
        self.assertIn(b"Ignored", body)

        good = b'{"ref":"refs/heads/' + hook.BRANCH.encode() + b'","pad":"' + b"b" * (3 * 1024 * 1024) + b'"}'
        with patch.object(hook, "_deploy", return_value=("200 OK", "deployed")) as deploy:
            status, body = self.request(body=good)
            self.assertEqual(status, "200 OK")
            self.assertIn(b"queued", body.lower())
            self.assertTrue(hook.join_deploy(timeout=5))
            deploy.assert_called_once()

    def test_chunked_body_without_content_length(self):
        body = json.dumps({"zen": "chunked"}).encode()
        status, out = self.request(body=body, event="ping", CONTENT_LENGTH=None)
        self.assertEqual(status, "200 OK")
        self.assertIn(b"pong", out)

    def test_rejected_and_ignored_requests_never_deploy(self):
        cases = [({"signature": "sha256=bad"}, "401"),
                 ({"body": b"{"}, "400"), ({"body": b"[]"}, "400"),
                 ({"event": "ping"}, "200"), ({"event": "issues"}, "200"),
                 ({"body": b'{"ref":"refs/heads/other"}'}, "200"),
                 ({"body": json.dumps({"ref": f"refs/heads/{hook.BRANCH}", "deleted": True}).encode()}, "200"),
                 ({"method": "DELETE"}, "405"),
                 ({"CONTENT_LENGTH": "-1"}, "400"),
                 ({"CONTENT_LENGTH": "0"}, "400"),
                 ({"CONTENT_LENGTH": str(hook.MAX_BODY + 1)}, "413")]
        with patch.object(hook, "_deploy") as deploy:
            for kwargs, expected in cases:
                with self.subTest(kwargs=kwargs):
                    self.assertTrue(self.request(**kwargs)[0].startswith(expected))
            self.assertTrue(hook.join_deploy(timeout=5))
            deploy.assert_not_called()

    def test_public_health_does_not_expose_configuration(self):
        status, body = self.request(method="GET")
        self.assertEqual(status, "200 OK")
        self.assertIn(b"reachable", body)
        self.assertNotIn(str(hook.REPO_DIR).encode(), body)
        self.assertNotIn(b"test-secret", body)

    def test_status_endpoint_reports_last_deploy_without_paths(self):
        with patch.object(hook, "_deploy",
                          return_value=("200 OK", "Deployed abc123; WSGI reload requested.")):
            self.request()
            self.assertTrue(hook.join_deploy(timeout=5))
        status, body = self.request(method="GET", PATH_INFO="/deploy/status")
        self.assertEqual(status, "200 OK")
        self.assertIn(b"Deployed abc123", body)
        self.assertNotIn(str(hook.REPO_DIR).encode(), body)
        self.assertNotIn(b"test-secret", body)

    def test_missing_secret(self):
        with patch.object(hook, "_secret", return_value=""):
            statuses = []
            hook.application({"REQUEST_METHOD": "POST"}, lambda s, h: statuses.append(s))
            self.assertTrue(statuses[0].startswith("503"))

    def test_dispatch(self):
        fallback = lambda e, s: [b"app"]
        wrapped = hook.wrap_application(fallback)
        self.assertEqual(wrapped({"PATH_INFO": "/deploy-other"}, None), [b"app"])
        self.assertIn(b"reachable", b"".join(wrapped(
            {"PATH_INFO": "/deploy/health", "REQUEST_METHOD": "GET"}, lambda s, h: None)))
        self.assertIn(b"reachable", b"".join(wrapped(
            {"PATH_INFO": "/deploy/status", "REQUEST_METHOD": "GET"}, lambda s, h: None)))

    def test_checkout_guards_and_successful_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            wsgi = Path(directory) / "web_wsgi.py"
            wsgi.touch()
            with patch.object(hook, "REPO_DIR", Path(directory)), patch.object(hook, "WSGI_FILE", str(wsgi)):
                for outputs, expected in [(["wrong"], "409"),
                                          ([hook.BRANCH, " M file.py"], "409"),
                                          ([hook.BRANCH, "", "Already up to date", "abc123"], "200")]:
                    with patch.object(hook, "_run_git", side_effect=outputs) as git, patch.object(hook.os, "utime") as touch:
                        self.assertTrue(hook._deploy()[0].startswith(expected))
                        if expected == "200":
                            touch.assert_called_once_with(str(wsgi), None)
                            self.assertIn(unittest.mock.call(
                                "pull", "--ff-only", "origin", hook.BRANCH,
                                timeout=hook.GIT_TIMEOUT_PULL), git.call_args_list)
                        else:
                            touch.assert_not_called()

    def test_missing_wsgi_does_not_pull(self):
        with patch.object(hook, "WSGI_FILE", ""), patch.object(hook, "_run_git") as git:
            self.assertTrue(hook._deploy()[0].startswith("503"))
            git.assert_not_called()

    def test_burst_of_pushes_coalesces_and_lands_latest(self):
        """Several pushes during one pull must not be lost: the worker re-runs
        and every acknowledgement is still immediate."""
        calls = []

        def deploy():
            calls.append(1)
            return "200 OK", f"deployed {len(calls)}"

        with patch.object(hook, "_deploy", side_effect=deploy):
            for _ in range(3):
                status, _ = self.request()
                self.assertEqual(status, "200 OK")
            self.assertTrue(hook.join_deploy(timeout=10))
        self.assertGreaterEqual(len(calls), 1)
        # the final pull always happens after the last push was queued
        self.assertTrue(hook.read_status()["detail"].startswith("deployed"))


if __name__ == "__main__":
    unittest.main()
