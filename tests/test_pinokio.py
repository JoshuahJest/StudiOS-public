import tempfile
import unittest
from pathlib import Path

from studios_coordination.pinokio import PinokioAppSpec, PinokioPolicyError, plan_lifecycle_action


class PinokioLifecycleTests(unittest.TestCase):
    def _spec(self, home: Path, **changes) -> PinokioAppSpec:
        app_dir = home / "api" / "phosphene.git"
        app_dir.mkdir(parents=True)
        (app_dir / "start.js").write_text("module.exports = {}", encoding="utf-8")
        values = {
            "app_id": "phosphene",
            "app_dir": app_dir,
            "revision": "a" * 40,
            "start_script": "start.js",
            "service_url": "http://127.0.0.1:8198",
        }
        values.update(changes)
        return PinokioAppSpec(**values)

    def test_plans_start_for_pinned_app_inside_managed_root(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)

            plan = plan_lifecycle_action(self._spec(home), "start", home)

            self.assertEqual(plan.http_method, "GET")
            self.assertEqual(plan.cwd, home / "api" / "phosphene.git")
            self.assertEqual(plan.executed, False)

    def test_plans_pinokio_loopback_run_url_without_a_shell_runner(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)

            plan = plan_lifecycle_action(self._spec(home), "start", home)

            self.assertEqual(
                plan.request_url,
                "http://127.0.0.1:42000/run/api/phosphene.git/start.js",
            )
            self.assertEqual(plan.http_method, "GET")

    def test_refuses_install_update_and_reset_actions(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            spec = self._spec(home)

            for action in ("install", "update", "reset"):
                with self.subTest(action=action):
                    with self.assertRaisesRegex(PinokioPolicyError, "action-not-allowlisted"):
                        plan_lifecycle_action(spec, action, home)

    def test_refuses_non_loopback_service(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            spec = self._spec(home, service_url="http://0.0.0.0:9000")

            with self.assertRaisesRegex(PinokioPolicyError, "service-not-loopback"):
                plan_lifecycle_action(spec, "start", home)

    def test_refuses_unpinned_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            spec = self._spec(home, revision="main")

            with self.assertRaisesRegex(PinokioPolicyError, "revision-not-pinned"):
                plan_lifecycle_action(spec, "start", home)

    def test_refuses_remote_pinokio_control_plane(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)

            with self.assertRaisesRegex(PinokioPolicyError, "control-plane-not-loopback"):
                plan_lifecycle_action(
                    self._spec(home),
                    "start",
                    home,
                    pinokio_base_url="http://enki-ii:42000",
                )


if __name__ == "__main__":
    unittest.main()
