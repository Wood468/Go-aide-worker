"""Static safety gates for the prototype; not a substitute for runtime isolation tests."""
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG = (ROOT / "addon/config.yaml").read_text()
DOCKERFILE = (ROOT / "addon/Dockerfile").read_text()
STARTUP = (ROOT / "addon/run.sh").read_text()


class ScaffoldSafety(unittest.TestCase):
    def test_no_privileged_integrations(self):
        for line in (
            "hassio_api: false", "homeassistant_api: false",
            "docker_api: false", "host_network: false",
            "full_access: false", "boot: manual", "map: []",
            "privileged: []",
        ):
            self.assertIn(line, CONFIG)

    def test_no_secrets_in_image(self):
        self.assertNotIn("GEMINI_API_KEY=", DOCKERFILE)
        self.assertNotIn("GITHUB_TOKEN=", DOCKERFILE)
        self.assertNotIn("COPY .env", DOCKERFILE)

    def test_idle_entrypoint(self):
        self.assertIn("exec sleep infinity", STARTUP)
        self.assertNotIn("aider ", STARTUP)

    def test_expected_files_exist(self):
        for name in ("addon/config.yaml", "addon/Dockerfile", "addon/run.sh", ".gitignore"):
            self.assertTrue((ROOT / name).is_file())


if __name__ == "__main__":
    unittest.main()
