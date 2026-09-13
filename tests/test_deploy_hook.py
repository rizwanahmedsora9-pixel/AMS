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
    def request(self, body=None, event="push", method="POST", signature=None, **extra):
        if body is None:
            body = json.dumps({"ref": f"refs/heads/{hook.BRANCH}"}).encode()
        env = {"REQUEST_METHOD": method, "CONTENT_LENGTH": str(len(body)),
               "wsgi.input": io.BytesIO(body), "HTTP_X_GITHUB_EVENT": event,
               "HTTP_X_HUB_SIGNATURE_256": signature if signature is not None else
               "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()}
        env.update(extra)
        statuses = []
        with patch.object(hook, "_secret", return_value="test-secret"):
            result = hook.application(env, lambda status, headers: statuses.append(status))
        return statuses[0], b"".join(result)

    def test_signed_push(self):
        with patch.object(hook, "_deploy", return_value=("200 OK", "deployed")) as deploy:
            self.assertEqual(self.request()[0], "200 OK")
            deploy.assert_called_once()

    def test_rejected_and_ignored_requests_never_deploy(self):
        cases = [({"signature": "sha256=bad"}, "401"),
                 ({"body": b"{"}, "400"), ({"body": b"[]"}, "400"),
                 ({"event": "ping"}, "200"), ({"event": "issues"}, "200"),
                 ({"body": b'{"ref":"refs/heads/other"}'}, "200"),
                 ({"body": json.dumps({"ref": f"refs/heads/{hook.BRANCH}", "deleted": True}).encode()}, "200"),
                 ({"method": "DELETE"}, "405"),
                 ({"CONTENT_LENGTH": "-1"}, "400"),
                 ({"CONTENT_LENGTH": str(hook.MAX_BODY + 1)}, "413")]
        with patch.object(hook, "_deploy") as deploy:
            for kwargs, expected in cases:
                with self.subTest(kwargs=kwargs):
                    self.assertTrue(self.request(**kwargs)[0].startswith(expected))
            deploy.assert_not_called()

    def test_public_health_does_not_expose_configuration(self):
        status, body = self.request(method="GET")
        self.assertEqual(status, "200 OK")
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
                            self.assertIn(unittest.mock.call("pull", "--ff-only", "origin", hook.BRANCH), git.call_args_list)
                        else:
                            touch.assert_not_called()

    def test_missing_wsgi_does_not_pull(self):
        with patch.object(hook, "WSGI_FILE", ""), patch.object(hook, "_run_git") as git:
            self.assertTrue(hook._deploy()[0].startswith("503"))
            git.assert_not_called()


if __name__ == "__main__":
    unittest.main()
