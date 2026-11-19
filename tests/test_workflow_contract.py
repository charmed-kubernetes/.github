from __future__ import annotations

import re
import unittest
from pathlib import Path


class TestWorkflowContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repo_root = Path(__file__).resolve().parent.parent
        cls.workflow_path = cls.repo_root / ".github" / "workflows" / "sync-teams.yml"
        if not cls.workflow_path.exists():
            cls.content = ""
        else:
            cls.content = cls.workflow_path.read_text(encoding="utf-8")

    def test_workflow_file_exists(self) -> None:
        self.assertTrue(
            self.workflow_path.exists(),
            f"Workflow file does not exist at {self.workflow_path}",
        )
        self.assertGreater(len(self.content.strip()), 0, "Workflow file must not be empty")

    def test_permissions_contents_read(self) -> None:
        self.test_workflow_file_exists()
        self.assertIn("permissions:", self.content)
        self.assertRegex(
            self.content,
            r"permissions:\s*\n\s+contents:\s*read",
            "Top-level permissions must specify contents: read",
        )

    def test_concurrency_settings(self) -> None:
        self.test_workflow_file_exists()
        self.assertIn("concurrency:", self.content)
        self.assertIn("group: sync-canonical-teams", self.content)
        self.assertIn("cancel-in-progress: false", self.content)

    def test_triggers(self) -> None:
        self.test_workflow_file_exists()
        # schedule cron check
        self.assertRegex(
            self.content,
            r"cron:\s*['\"]15 \*/6 \* \* \*['\"]",
            "Schedule must have cron '15 */6 * * *'",
        )

        # workflow_dispatch inputs
        self.assertIn("workflow_dispatch:", self.content)
        self.assertRegex(
            self.content,
            r"dry_run:\s*[\r\n]+(?:\s+[^\r\n]+[\r\n]+)*?\s+type:\s*boolean",
            "workflow_dispatch must define dry_run boolean input",
        )
        self.assertRegex(
            self.content,
            r"allow_empty:\s*[\r\n]+(?:\s+[^\r\n]+[\r\n]+)*?\s+type:\s*boolean",
            "workflow_dispatch must define allow_empty boolean input",
        )

        # pull_request paths filter
        self.assertIn("pull_request:", self.content)
        self.assertIn("scripts/sync_teams.py", self.content)
        self.assertIn("tests/**", self.content)
        self.assertIn(".github/workflows/sync-teams.yml", self.content)

    def test_action_pinning_full_sha(self) -> None:
        self.test_workflow_file_exists()
        uses_lines = re.findall(r"uses:\s*([^\n]+)", self.content)
        self.assertGreater(len(uses_lines), 0, "Workflow must contain action uses steps")
        for line in uses_lines:
            action_ref = line.split("#")[0].strip()
            self.assertRegex(
                action_ref,
                r"^[^@\s]+@[0-9a-f]{40}$",
                f"Action {action_ref} is not pinned to a full 40-character commit SHA",
            )

    def test_jobs_structure_and_isolation(self) -> None:
        self.test_workflow_file_exists()
        self.assertIn("test:", self.content, "Workflow must define a 'test' job")
        self.assertIn("sync:", self.content, "Workflow must define a 'sync' job")

        # sync job must depend on test and not run on pull_request
        self.assertRegex(
            self.content,
            r"needs:\s*(?:\[\s*test\s*\]|\btest\b)",
            "sync job must declare 'needs: test' or 'needs: [test]'",
        )
        self.assertRegex(
            self.content,
            r"if:\s*github\.event_name\s*!=\s*['\"]pull_request['\"]",
            "sync job must be gated with 'if: github.event_name != \\'pull_request\\''",
        )

    def test_python_version_configuration(self) -> None:
        self.test_workflow_file_exists()
        self.assertRegex(
            self.content,
            r"python-version:\s*['\"]?3\.12['\"]?",
            "Workflow must configure python-version: '3.12'",
        )

    def test_sync_job_script_and_secret_guards(self) -> None:
        self.test_workflow_file_exists()
        # Secret check
        self.assertIn("CANONICAL_ORG_READ_TOKEN", self.content)
        self.assertIn("CK8S_ORG_SYNC_TOKEN", self.content)

        # Environment variable bindings for workflow inputs
        self.assertIn("INPUT_DRY_RUN: ${{ inputs.dry_run }}", self.content)
        self.assertIn("INPUT_ALLOW_EMPTY: ${{ inputs.allow_empty }}", self.content)

        # No direct ${{ inputs.* }} inside shell run blocks
        for part in self.content.split("run:")[1:]:
            script_chunk = part.split("\n\n")[0]
            self.assertNotIn(
                "${{ inputs.",
                script_chunk,
                "Workflow run block must not directly interpolate ${{ inputs.* }} into shell scripts",
            )

        # CLI invocation parameters
        self.assertIn("--source-org canonical", self.content)
        self.assertIn("--target-org charmed-kubernetes", self.content)
        self.assertIn("--teams kubernetes kubernetes-admins", self.content)
        self.assertIn("--dry-run", self.content)
        self.assertIn("--allow-empty", self.content)


if __name__ == "__main__":
    unittest.main()
