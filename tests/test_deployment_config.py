"""Static Docker deployment checks; no daemon, network, or secrets required."""

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DockerDeploymentConfigTests(unittest.TestCase):
    def read(self, relative_path: str) -> str:
        return (ROOT / relative_path).read_text(encoding="utf-8")

    def test_dockerfile_uses_non_root_runtime_and_does_not_copy_database(self):
        dockerfile = self.read("Dockerfile")
        self.assertIn("FROM python:3.12-slim-bookworm", dockerfile)
        self.assertIn("requirements.docker.lock", dockerfile)
        self.assertIn("USER issuepilot", dockerfile)
        self.assertIn("COPY data/issues_demo.json /app/seed/issues_demo.json", dockerfile)
        self.assertNotIn("tasks.sqlite3", dockerfile)
        self.assertIn("STOPSIGNAL SIGTERM", dockerfile)

    def test_compose_has_two_roles_one_image_and_isolated_shared_database(self):
        compose = self.read("compose.yaml")
        self.assertRegex(compose, r"(?m)^  api:\s*$")
        self.assertRegex(compose, r"(?m)^  worker:\s*$")
        self.assertEqual(compose.count("<<: *issuepilot-common"), 2)
        self.assertIn("image: issuepilot:0.1.0-compose", compose)
        self.assertNotIn("cache_to:", compose)
        self.assertIn('"127.0.0.1:8791:8790"', compose)
        self.assertIn("ISSUEPILOT_DB: /var/lib/issuepilot/tasks.sqlite3", compose)
        self.assertIn("issuepilot_data:/var/lib/issuepilot", compose)
        self.assertIn("${ISSUEPILOT_DATA_VOLUME:-issuepilot_compose_data}", compose)
        self.assertNotIn("./data:/var/lib/issuepilot", compose)
        self.assertIn('"issuepilot.api:app"', compose)
        self.assertIn('"issuepilot.worker"', compose)

    def test_compose_has_health_exit_and_bounded_logging_settings(self):
        compose = self.read("compose.yaml")
        self.assertEqual(compose.count("healthcheck:"), 2)
        self.assertIn("init: true", compose)
        self.assertIn("stop_grace_period: 60s", compose)
        self.assertIn("restart: unless-stopped", compose)
        self.assertIn("max-size: 10m", compose)
        self.assertIn('max-file: "3"', compose)
        self.assertIn("read_only: true", compose)

    def test_worker_handles_sigterm_without_changing_windows_start_command(self):
        worker = self.read("issuepilot/worker.py")
        self.assertIn("signal.signal(signal.SIGTERM, request_stop)", worker)
        self.assertIn("stop_requested.wait(args.poll_seconds)", worker)
        self.assertIn('if __name__ == "__main__":', worker)

    def test_acceptance_mock_worker_is_explicitly_gated(self):
        override = self.read("compose.acceptance.yaml")
        mock_worker = self.read("docker/mock_worker.py")
        self.assertIn('ISSUEPILOT_ACCEPTANCE_MOCK: "enabled"', override)
        self.assertIn('os.getenv("ISSUEPILOT_ACCEPTANCE_MOCK") != "enabled"', mock_worker)
        self.assertIn("sys.path.insert", mock_worker)
        self.assertIn("classify_without_network", mock_worker)
        self.assertNotIn("OpenAI", mock_worker)

    def test_dockerignore_excludes_sensitive_and_local_state(self):
        ignored = self.read(".dockerignore")
        for required in (".env", "docker.env", "**/privatecredentials*", "*.sqlite3", "logs/", "backups/", ".venv/", ".git/"):
            self.assertIn(required, ignored)
        self.assertIn("!data/issues_demo.json", ignored)
        self.assertNotIn("!data/issues_seed.json", ignored)

    def test_container_environment_example_contains_only_placeholders(self):
        values = {}
        for line in self.read("docker.env.example").splitlines():
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
        self.assertTrue(values["ISSUEPILOT_AUTH_SECRET"].startswith("replace-"))
        self.assertTrue(values["QWEN_API_KEY"].startswith("replace-"))
        self.assertEqual(values["ISSUEPILOT_GITHUB_TEST_REPO"], "")
        self.assertEqual(values["ISSUEPILOT_GITHUB_TOKEN"], "")
        self.assertFalse(any(re.search(r"(?:ghp_|github_pat_|sk-)[A-Za-z0-9_-]{16,}", value) for value in values.values()))

    def test_public_defaults_use_only_synthetic_dataset(self):
        config = self.read("issuepilot/config.py")
        compose = self.read("compose.yaml")
        demo = json.loads(self.read("data/issues_demo.json"))
        self.assertIn('"issues_demo.json"', config)
        self.assertNotIn('"issues_seed.json"', config)
        self.assertIn("/app/seed/issues_demo.json", compose)
        self.assertEqual(demo["dataset_kind"], "synthetic_demo")
        self.assertEqual(len(demo["items"]), 4)
        self.assertTrue(all(item["sample_group"].startswith("synthetic_") for item in demo["items"]))

    def test_gitignore_excludes_private_release_artifacts(self):
        ignored = self.read(".gitignore")
        for required in (
            ".env.*", "backups/", "logs/", "data/*", "*.sqlite3",
            "evaluation/*.json", "evaluation/qwen_baseline_*/",
            "evaluation/similarity_real_smoke_*/",
        ):
            self.assertIn(required, ignored)
        self.assertIn("!data/issues_demo.json", ignored)
        self.assertNotIn("!data/issues_seed.json", ignored)
