import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from scripts.sync_teams import (
    GitHubClient,
    GitHubError,
    TeamAction,
    TeamSyncResult,
    main,
    reconcile_team,
    render_markdown_summary,
)


class FakeHTTPResponse:
    def __init__(self, status: int, data: bytes, headers: dict[str, str] | None = None):
        self.status = status
        self.data = data
        self.headers = headers or {}

    def read(self) -> bytes:
        return self.data

    def getheader(self, name: str, default: str | None = None) -> str | None:
        for k, v in self.headers.items():
            if k.lower() == name.lower():
                return v
        return default

    def info(self):
        class Info:
            def __init__(self, headers: dict[str, str]):
                self._headers = headers

            def get(self, name: str, default: str | None = None) -> str | None:
                for k, v in self._headers.items():
                    if k.lower() == name.lower():
                        return v
                return default

            def items(self):
                return self._headers.items()

        return Info(self.headers)


class FakeOpener:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def open(self, req, timeout=None):
        method = req.get_method()
        url = req.full_url
        headers = dict(req.header_items())
        data = req.data
        body = json.loads(data.decode("utf-8")) if data else None
        self.calls.append({
            "method": method,
            "url": url,
            "headers": headers,
            "body": body,
        })
        return self.handler(method, url, headers, body)


class TestGitHubClient(unittest.TestCase):
    def test_pagination_multiple_pages(self):
        page1_data = json.dumps([{"login": "alice"}]).encode("utf-8")
        page2_data = json.dumps([{"login": "bob"}]).encode("utf-8")

        def handler(method, url, headers, body):
            if "page=1" in url or "per_page" in url and "page=2" not in url:
                link = '<https://api.github.com/orgs/canonical/members?page=2>; rel="next", <https://api.github.com/orgs/canonical/members?page=2>; rel="last"'
                return FakeHTTPResponse(200, page1_data, {"Link": link})
            elif "page=2" in url:
                link = '<https://api.github.com/orgs/canonical/members?page=1>; rel="prev"'
                return FakeHTTPResponse(200, page2_data, {"Link": link})
            raise AssertionError(f"Unexpected url: {url}")

        opener = FakeOpener(handler)
        client = GitHubClient(token="fake-secret-token", opener=opener)
        items = client.paginate("/orgs/canonical/members?per_page=1")
        self.assertEqual(len(items), 2)
        self.assertEqual([i["login"] for i in items], ["alice", "bob"])

    def test_transient_retry_and_auth_redaction(self):
        attempts = 0
        sleeps = []

        def fake_sleep(duration):
            sleeps.append(duration)

        def handler(method, url, headers, body):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                import urllib.error
                raise urllib.error.HTTPError(url, 503, "Service Unavailable", {}, io.BytesIO(b'{"message":"unavailable"}'))
            return FakeHTTPResponse(200, b'{"ok": true}', {})

        opener = FakeOpener(handler)
        client = GitHubClient(token="secret-token-12345", opener=opener, sleep_fn=fake_sleep)
        status, data, headers = client.request_json("GET", "/test")
        self.assertEqual(status, 200)
        self.assertEqual(data, {"ok": True})
        self.assertEqual(attempts, 3)
        self.assertEqual(sleeps, [1.0, 2.0])

    def test_forbidden_error_does_not_leak_token(self):
        def handler(method, url, headers, body):
            import urllib.error
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, io.BytesIO(b'{"message": "Must have admin rights to Repository."}'))

        opener = FakeOpener(handler)
        client = GitHubClient(token="super-secret-pat-xyz", opener=opener)
        with self.assertRaises(GitHubError) as ctx:
            client.request_json("GET", "/orgs/secret-org/teams")

        err = ctx.exception
        self.assertEqual(err.status, 403)
        self.assertNotIn("super-secret-pat-xyz", str(err))
        self.assertNotIn("super-secret-pat-xyz", repr(err))
        self.assertIn("Must have admin rights to Repository.", str(err))


class TestReconcileTeam(unittest.TestCase):
    def setUp(self):
        self.source_calls = []
        self.target_calls = []

    def make_clients(self, source_handler, target_handler):
        source_opener = FakeOpener(source_handler)
        target_opener = FakeOpener(target_handler)
        source_client = GitHubClient("src-token", opener=source_opener)
        target_client = GitHubClient("tgt-token", opener=target_opener)
        return source_client, target_client, source_opener, target_opener

    def test_target_team_not_found_creates_team(self):
        def source_handler(method, url, headers, body):
            if "role=member" in url:
                return FakeHTTPResponse(200, json.dumps([{"login": "alice"}]).encode())
            if "role=maintainer" in url:
                return FakeHTTPResponse(200, json.dumps([]).encode())
            return FakeHTTPResponse(200, b"[]")

        def target_handler(method, url, headers, body):
            import urllib.error
            if method == "GET" and url.endswith("/teams/kubernetes"):
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b'{"message": "Not Found"}'))
            if method == "POST" and url.endswith("/orgs/charmed-kubernetes/teams"):
                return FakeHTTPResponse(201, json.dumps({"slug": "kubernetes", "name": "kubernetes"}).encode())
            if method == "GET" and "/members?" in url:
                return FakeHTTPResponse(200, b"[]")
            if method == "GET" and "/invitations?" in url:
                return FakeHTTPResponse(200, b"[]")
            if method == "PUT" and "/memberships/alice" in url:
                return FakeHTTPResponse(200, json.dumps({"state": "active", "role": "member"}).encode())
            raise AssertionError(f"Unexpected target call: {method} {url}")

        source_client, target_client, _, target_opener = self.make_clients(source_handler, target_handler)
        result = reconcile_team(
            source_client, target_client,
            "canonical", "charmed-kubernetes", "kubernetes",
            target_org_owners=set(),
            dry_run=False,
        )
        self.assertTrue(result.created_team)
        self.assertEqual(len(result.errors), 0)
        self.assertTrue(any(c["method"] == "POST" and c["url"].endswith("/orgs/charmed-kubernetes/teams") for c in target_opener.calls))
        self.assertTrue(any(a.action == "add_member" and a.username == "alice" for a in result.actions))

    def test_target_team_not_found_dry_run_skips_mutations(self):
        def source_handler(method, url, headers, body):
            if "role=member" in url:
                return FakeHTTPResponse(200, json.dumps([{"login": "alice"}]).encode())
            return FakeHTTPResponse(200, b"[]")

        def target_handler(method, url, headers, body):
            import urllib.error
            if method == "GET" and url.endswith("/teams/kubernetes"):
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b'{"message": "Not Found"}'))
            raise AssertionError(f"No other target call expected in dry-run for missing team: {method} {url}")

        source_client, target_client, _, target_opener = self.make_clients(source_handler, target_handler)
        result = reconcile_team(
            source_client, target_client,
            "canonical", "charmed-kubernetes", "kubernetes",
            target_org_owners=set(),
            dry_run=True,
        )
        self.assertTrue(result.created_team)
        self.assertEqual(len(result.errors), 0)
        # In dry run with missing team, target team members are treated as empty, actions contain add_member for alice
        self.assertTrue(any(a.action == "add_member" and a.username == "alice" for a in result.actions))
        self.assertEqual(len(target_opener.calls), 1)  # only the initial GET

    def test_empty_source_circuit_breaker(self):
        def source_handler(method, url, headers, body):
            return FakeHTTPResponse(200, b"[]")

        def target_handler(method, url, headers, body):
            raise AssertionError("Target should not be called when circuit breaker trips")

        source_client, target_client, _, target_opener = self.make_clients(source_handler, target_handler)
        with self.assertRaises(RuntimeError) as ctx:
            reconcile_team(
                source_client, target_client,
                "canonical", "charmed-kubernetes", "kubernetes",
                target_org_owners=set(),
                dry_run=False,
                allow_empty=False,
            )
        self.assertIn("circuit breaker", str(ctx.exception).lower())
        self.assertEqual(len(target_opener.calls), 0)

    def test_empty_source_allowed_with_flag(self):
        def source_handler(method, url, headers, body):
            return FakeHTTPResponse(200, b"[]")

        def target_handler(method, url, headers, body):
            if method == "GET" and url.endswith("/teams/kubernetes"):
                return FakeHTTPResponse(200, json.dumps({"slug": "kubernetes"}).encode())
            if method == "GET" and "role=member" in url:
                return FakeHTTPResponse(200, json.dumps([{"login": "olduser"}]).encode())
            if method == "GET" and "role=maintainer" in url:
                return FakeHTTPResponse(200, b"[]")
            if method == "GET" and "/invitations?" in url:
                return FakeHTTPResponse(200, b"[]")
            if method == "DELETE" and url.endswith("/memberships/olduser"):
                return FakeHTTPResponse(204, b"")
            raise AssertionError(f"Unexpected target call: {method} {url}")

        source_client, target_client, _, target_opener = self.make_clients(source_handler, target_handler)
        result = reconcile_team(
            source_client, target_client,
            "canonical", "charmed-kubernetes", "kubernetes",
            target_org_owners=set(),
            dry_run=False,
            allow_empty=True,
        )
        self.assertEqual(result.desired_count, 0)
        self.assertTrue(any(a.action == "remove_member" and a.username == "olduser" for a in result.actions))

    def test_full_reconciliation_logic(self):
        # Desired:
        # - "Alice" (member in source, new in target -> add_member)
        # - "Bob" (maintainer in source, member in target -> update_role to maintainer)
        # - "Carol" (member in source, maintainer in target, BUT is target org owner -> noop_org_owner)
        # - "Dave" (member in source, maintainer in target, not org owner -> update_role to member)
        # - "Eve" (member in source, pending in target with role member -> skip_pending)
        # - "Frank" (maintainer in source, pending in target with role member -> update_pending_role to maintainer)
        # Target Active to remove:
        # - "Mallory" (not in source -> remove_member)
        # Target Pending to remove:
        # - "Peggy" (not in source, team_count == 1 -> remove_pending + cancel org invite)
        # - "Sybil" (not in source, team_count == 2 -> remove_pending only)
        def source_handler(method, url, headers, body):
            if "role=member" in url:
                return FakeHTTPResponse(200, json.dumps([
                    {"login": "Alice"},
                    {"login": "Carol"},
                    {"login": "Dave"},
                    {"login": "Eve"},
                    {"login": "Frank"},  # Frank appears in both; maintainer must win
                ]).encode())
            if "role=maintainer" in url:
                return FakeHTTPResponse(200, json.dumps([
                    {"login": "Bob"},
                    {"login": "Frank"},
                ]).encode())
            return FakeHTTPResponse(200, b"[]")

        def target_handler(method, url, headers, body):
            if method == "GET" and url.endswith("/teams/kubernetes"):
                return FakeHTTPResponse(200, json.dumps({"slug": "kubernetes"}).encode())
            if method == "GET" and "/teams/kubernetes/members?role=member" in url:
                return FakeHTTPResponse(200, json.dumps([
                    {"login": "Bob"},
                    {"login": "Mallory"},
                ]).encode())
            if method == "GET" and "/teams/kubernetes/members?role=maintainer" in url:
                return FakeHTTPResponse(200, json.dumps([
                    {"login": "Carol"},
                    {"login": "Dave"},
                ]).encode())
            if method == "GET" and "/teams/kubernetes/invitations" in url:
                return FakeHTTPResponse(200, json.dumps([
                    {"login": "Eve", "id": 101, "team_count": 1},
                    {"login": "Frank", "id": 102, "team_count": 1},
                    {"login": "Peggy", "id": 103, "team_count": 1},
                    {"login": "Sybil", "id": 104, "team_count": 2},
                    {"login": None, "id": 105, "team_count": 1},  # must skip None login
                ]).encode())
            if method == "GET" and "/memberships/Eve" in url:
                return FakeHTTPResponse(200, json.dumps({"state": "pending", "role": "member"}).encode())
            if method == "GET" and "/memberships/Frank" in url:
                return FakeHTTPResponse(200, json.dumps({"state": "pending", "role": "member"}).encode())
            if method == "PUT" and "/memberships/Alice" in url:
                return FakeHTTPResponse(200, json.dumps({"role": "member"}).encode())
            if method == "PUT" and "/memberships/Bob" in url:
                return FakeHTTPResponse(200, json.dumps({"role": "maintainer"}).encode())
            if method == "PUT" and "/memberships/Dave" in url:
                return FakeHTTPResponse(200, json.dumps({"role": "member"}).encode())
            if method == "PUT" and "/memberships/Frank" in url:
                return FakeHTTPResponse(200, json.dumps({"role": "maintainer"}).encode())
            if method == "DELETE" and "/memberships/Mallory" in url:
                return FakeHTTPResponse(204, b"")
            if method == "DELETE" and "/memberships/Peggy" in url:
                return FakeHTTPResponse(204, b"")
            if method == "DELETE" and url.endswith("/orgs/charmed-kubernetes/invitations/103"):
                return FakeHTTPResponse(204, b"")
            if method == "DELETE" and "/memberships/Sybil" in url:
                return FakeHTTPResponse(204, b"")
            raise AssertionError(f"Unexpected target call: {method} {url}")

        source_client, target_client, _, target_opener = self.make_clients(source_handler, target_handler)
        result = reconcile_team(
            source_client, target_client,
            "canonical", "charmed-kubernetes", "kubernetes",
            target_org_owners={"carol"},  # carol is org owner
            dry_run=False,
        )
        self.assertEqual(len(result.errors), 0)
        self.assertEqual(result.desired_count, 6)

        actions_by_type = {}
        for a in result.actions:
            actions_by_type.setdefault(a.action, []).append(a)

        self.assertEqual([a.username for a in actions_by_type.get("add_member", [])], ["Alice"])
        self.assertEqual([a.username for a in actions_by_type.get("update_role", [])], ["Bob", "Dave"])
        self.assertEqual([a.username for a in actions_by_type.get("noop_org_owner", [])], ["Carol"])
        self.assertEqual([a.username for a in actions_by_type.get("skip_pending", [])], ["Eve"])
        self.assertEqual([a.username for a in actions_by_type.get("update_pending_role", [])], ["Frank"])
        self.assertEqual([a.username for a in actions_by_type.get("remove_member", [])], ["Mallory"])
        self.assertEqual([a.username for a in actions_by_type.get("remove_pending", [])], ["Peggy", "Sybil"])

        # Check that invitation 103 was deleted but 104 was not
        cancelled_invites = [c["url"] for c in target_opener.calls if "invitations/" in c["url"] and c["method"] == "DELETE"]
        self.assertTrue(any("103" in u for u in cancelled_invites))
        self.assertFalse(any("104" in u for u in cancelled_invites))


class TestSummaryAndCLI(unittest.TestCase):
    def test_render_markdown_summary(self):
        actions = [
            TeamAction("add_member", "alice", "member"),
            TeamAction("update_role", "bob", "maintainer"),
            TeamAction("noop_org_owner", "carol", "member", "org owner coerced to maintainer by GitHub"),
            TeamAction("skip_pending", "eve", "member"),
            TeamAction("remove_member", "mallory"),
            TeamAction("remove_pending", "peggy"),
        ]
        res = TeamSyncResult(
            team_slug="kubernetes",
            created_team=True,
            actions=actions,
            errors=[],
            desired_count=4,
        )
        summary_dry = render_markdown_summary([res], dry_run=True)
        self.assertIn("DRY RUN", summary_dry)
        self.assertIn("kubernetes", summary_dry)
        self.assertIn("alice", summary_dry)
        self.assertIn("Would create team", summary_dry)

        summary_live = render_markdown_summary([res], dry_run=False)
        self.assertNotIn("DRY RUN", summary_live)
        self.assertIn("Created team", summary_live)
    def test_missing_env_token_exits_2(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch("sys.stderr", new_callable=io.StringIO) as fake_err:
                code = main(["--dry-run"])
                self.assertEqual(code, 2)
                self.assertIn("CANONICAL_ORG_READ_TOKEN", fake_err.getvalue())

    def test_main_cli_step_summary_output(self):
        with tempfile.NamedTemporaryFile("w+", delete=False) as tf:
            summary_path = tf.name

        def source_handler(method, url, headers, body):
            return FakeHTTPResponse(200, json.dumps([{"login": "alice"}]).encode())

        def target_handler(method, url, headers, body):
            if "/members?role=admin" in url:
                return FakeHTTPResponse(200, b"[]")
            if url.endswith("/teams/kubernetes"):
                return FakeHTTPResponse(200, json.dumps({"slug": "kubernetes"}).encode())
            if "/members?" in url:
                return FakeHTTPResponse(200, json.dumps([{"login": "alice"}]).encode())
            if "/invitations?" in url:
                return FakeHTTPResponse(200, b"[]")
            raise AssertionError(f"Unexpected target call: {method} {url}")

        source_opener = FakeOpener(source_handler)
        target_opener = FakeOpener(target_handler)

        def mock_client_init(self, token, base_url="https://api.github.com", opener=None, sleep_fn=None):
            if token == "src_tok":
                self.opener = source_opener
            else:
                self.opener = target_opener
            self.token = token
            self.base_url = base_url.rstrip("/")
            self.sleep_fn = sleep_fn or (lambda x: None)

        env = {
            "CANONICAL_ORG_READ_TOKEN": "src_tok",
            "CK8S_ORG_SYNC_TOKEN": "tgt_tok",
            "GITHUB_STEP_SUMMARY": summary_path,
        }

        try:
            with patch.dict(os.environ, env, clear=True):
                with patch.object(GitHubClient, "__init__", mock_client_init):
                    with patch("sys.stdout", new_callable=io.StringIO) as fake_out:
                        code = main(["--teams", "kubernetes", "--dry-run"])
                        self.assertEqual(code, 0)
                        stdout_val = fake_out.getvalue()
                        self.assertIn("Canonical -> Charmed Kubernetes Team Sync", stdout_val)
                        with open(summary_path, "r", encoding="utf-8") as f:
                            file_val = f.read()
                        self.assertEqual(stdout_val.strip(), file_val.strip())
        finally:
            if os.path.exists(summary_path):
                os.remove(summary_path)

    def test_partial_failure_continues_and_exits_1(self):
        def source_handler(method, url, headers, body):
            if "role=member" in url:
                return FakeHTTPResponse(200, json.dumps([{"login": "user1"}, {"login": "user2"}]).encode())
            return FakeHTTPResponse(200, b"[]")

        def target_handler(method, url, headers, body):
            import urllib.error
            if "/members?role=admin" in url:
                return FakeHTTPResponse(200, b"[]")
            if url.endswith("/teams/kubernetes"):
                return FakeHTTPResponse(200, json.dumps({"slug": "kubernetes"}).encode())
            if "/members?" in url:
                return FakeHTTPResponse(200, b"[]")
            if "/invitations?" in url:
                return FakeHTTPResponse(200, b"[]")
            if "/memberships/user1" in url:
                raise urllib.error.HTTPError(url, 422, "Unprocessable Entity", {}, io.BytesIO(b'{"message": "Validation failed"}'))
            if "/memberships/user2" in url:
                return FakeHTTPResponse(200, json.dumps({"role": "member"}).encode())
            raise AssertionError(f"Unexpected target call: {method} {url}")

        source_opener = FakeOpener(source_handler)
        target_opener = FakeOpener(target_handler)

        def mock_client_init(self, token, base_url="https://api.github.com", opener=None, sleep_fn=None):
            self.opener = source_opener if token == "src_tok" else target_opener
            self.token = token
            self.base_url = base_url.rstrip("/")
            self.sleep_fn = sleep_fn or (lambda x: None)

        env = {"CANONICAL_ORG_READ_TOKEN": "src_tok", "CK8S_ORG_SYNC_TOKEN": "tgt_tok"}
        with patch.dict(os.environ, env, clear=True):
            with patch.object(GitHubClient, "__init__", mock_client_init):
                with patch("sys.stdout", new_callable=io.StringIO) as fake_out:
                    code = main(["--teams", "kubernetes"])
                    self.assertEqual(code, 1)
                    stdout_val = fake_out.getvalue()
                    self.assertIn("Validation failed", stdout_val)
                    self.assertIn("user2", stdout_val)

    def test_circuit_breaker_in_main_exits_1(self):
        def source_handler(method, url, headers, body):
            return FakeHTTPResponse(200, b"[]")

        def target_handler(method, url, headers, body):
            if "/members?role=admin" in url:
                return FakeHTTPResponse(200, b"[]")
            raise AssertionError(f"Target should not be called: {method} {url}")

        source_opener = FakeOpener(source_handler)
        target_opener = FakeOpener(target_handler)

        def mock_client_init(self, token, base_url="https://api.github.com", opener=None, sleep_fn=None):
            self.opener = source_opener if token == "src_tok" else target_opener
            self.token = token
            self.base_url = base_url.rstrip("/")
            self.sleep_fn = sleep_fn or (lambda x: None)

        env = {"CANONICAL_ORG_READ_TOKEN": "src_tok", "CK8S_ORG_SYNC_TOKEN": "tgt_tok"}
        with patch.dict(os.environ, env, clear=True):
            with patch.object(GitHubClient, "__init__", mock_client_init):
                with patch("sys.stdout", new_callable=io.StringIO) as fake_out:
                    code = main(["--teams", "kubernetes"])
                    self.assertEqual(code, 1)
                    stdout_val = fake_out.getvalue()
                    self.assertIn("Circuit breaker tripped", stdout_val)

if __name__ == "__main__":
    unittest.main()
