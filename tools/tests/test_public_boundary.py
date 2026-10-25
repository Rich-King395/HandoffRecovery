"""Offline public-boundary checks; no Android, model SDK, or credentials."""
import importlib.util
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "check_public_boundary.py"
spec = importlib.util.spec_from_file_location("public_boundary", SOURCE)
boundary = importlib.util.module_from_spec(spec)
spec.loader.exec_module(boundary)
POLICY = SOURCE.parents[1] / ".public-release.json"


class PublicBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / boundary.POLICY_NAME).write_bytes(POLICY.read_bytes())

    def put(self, name, text="public content"):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def rows(self):
        return {r["path"]: r for r in boundary.audit(self.root)["files"]}

    def test_clean_tree_needs_no_device_credentials_or_extra_files(self):
        self.put("README.md")
        self.assertTrue(boundary.audit(self.root)["ready"])

    def test_runtime_prompts_are_public(self):
        public = json.loads(POLICY.read_text())["public_directories"]
        directory = "resumebench/models/prompts" if "resumebench" in public else "src/handoff_recovery/planner"
        name = directory + "/system_prompt.txt"
        self.put(name)
        self.assertEqual(self.rows()[name]["decision"], "include")

    def test_guidance_is_excluded_at_any_depth(self):
        for name in ["AGENTS.md", "docs/AGENTS.md", "HANDOFF_CODEX_PROMPT.txt", ".codex/settings.json"]:
            self.put(name)
        rows = self.rows()
        for name in ["AGENTS.md", "docs/AGENTS.md", "HANDOFF_CODEX_PROMPT.txt"]:
            self.assertEqual(rows[name]["decision"], "exclude")
        self.assertEqual(rows[".codex"]["decision"], "exclude")

    def test_excluded_contents_are_never_opened(self):
        self.put(".env", "do not inspect")
        self.put("results/run/reply.json", "do not inspect")
        read = Path.read_bytes
        def guarded(path):
            if path.name == ".env" or "results" in path.parts:
                self.fail("Excluded private content was read")
            return read(path)
        with patch.object(Path, "read_bytes", guarded):
            rows = self.rows()
        self.assertEqual(rows[".env"]["decision"], "exclude")
        self.assertEqual(rows["results"]["decision"], "exclude")

    def test_only_root_env_template_is_allowed(self):
        self.put(".env.example", "API_KEY=\n")
        self.put("docs/.env.example", "")
        self.assertEqual(self.rows()[".env.example"]["decision"], "include")
        self.assertEqual(self.rows()["docs/.env.example"]["decision"], "exclude")

    def test_env_template_must_have_empty_or_placeholder_credentials(self):
        self.put(".env.example", "API_KEY=set-locally-do-not-commit\n")
        self.assertIn("nonplaceholder_template_credential", self.rows()[".env.example"]["reasons"])

    def test_empty_template_key_does_not_consume_the_next_line(self):
        self.put(".env.example", "API_KEY=\n# Configure your provider\nMODEL=example-model\n")
        self.assertEqual(self.rows()[".env.example"]["decision"], "include")

    def test_nested_results_are_not_public_source(self):
        self.put("scripts/results/model-response.json", "do not inspect")
        self.assertEqual(self.rows()["scripts/results"]["decision"], "exclude")

    def test_literal_credentials_are_reviewed_even_in_tests(self):
        public = json.loads(POLICY.read_text())["public_directories"]
        directory = "resumebench" if "resumebench" in public else "src"
        name = directory + "/client.py"
        self.put(name, 'api_key="' + 'a' * 32 + '"')
        self.assertIn("credential_pattern_review", self.rows()[name]["reasons"])

    def test_backup_source_copies_are_not_public(self):
        name = "tests/helper.py.bak.20260101"
        self.put(name)
        self.assertEqual(self.rows()[name]["decision"], "exclude")

    def test_unknown_personal_file_is_not_implicitly_public(self):
        self.put("my_notes.md")
        self.assertEqual(self.rows()["my_notes.md"]["reasons"], ["not_allowlisted"])

    def test_recorded_fixtures_require_review(self):
        self.put("tests/fixtures/recorded_inputs/state.json", "{}")
        self.assertIn("fixture_provenance_review", self.rows()["tests/fixtures/recorded_inputs/state.json"]["reasons"])

    def test_personal_paths_and_experiment_links_require_review(self):
        self.put("README.md", "/home" + "/example/project\n" + ".runtime" + "/maintenance/record\n")
        self.assertEqual(self.rows()["README.md"]["decision"], "review")

    def test_symlink_is_not_followed(self):
        target = self.put("README.md")
        (self.root / "PUBLIC_RELEASE.md").symlink_to(target)
        self.assertEqual(self.rows()["PUBLIC_RELEASE.md"]["reasons"], ["symlink"])

    def approve(self, name, reasons):
        digest = hashlib.sha256((self.root / name).read_bytes()).hexdigest()
        self.put(".public-release-reviews.json", json.dumps({"version": 1, "files": {
            name: {"sha256": digest, "reasons": reasons, "provenance": "Synthetic test input"}}}))

    def test_reviewed_fixture_is_bound_to_exact_content(self):
        name = "tests/fixtures/state.json"
        self.put(name, "{}")
        self.approve(name, ["fixture_provenance_review"])
        self.assertEqual(self.rows()[name]["decision"], "include")
        self.put(name, '{"changed": true}')
        self.assertEqual(self.rows()[name]["decision"], "review")

    def test_review_cannot_approve_secrets_or_excluded_files(self):
        name = "tests/fixtures/state.json"
        self.put(name, 'api_key="' + 'a' * 32 + '"')
        self.approve(name, ["fixture_provenance_review", "credential_pattern_review"])
        self.assertEqual(self.rows()[name]["decision"], "review")
        self.put("AGENTS.md")
        self.approve("AGENTS.md", ["assistant_or_host_guidance"])
        self.assertEqual(self.rows()["AGENTS.md"]["decision"], "exclude")

    def test_tracked_guidance_stays_blocked_even_if_ignored(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.put("AGENTS.md")
        subprocess.run(["git", "-C", str(self.root), "add", "AGENTS.md"], check=True)
        self.put(".gitignore", "AGENTS.md\n")
        self.assertEqual(self.rows()["AGENTS.md"]["decision"], "exclude")
        self.assertFalse(boundary.audit(self.root)["ready"])


if __name__ == "__main__":
    unittest.main()
