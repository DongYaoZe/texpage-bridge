from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
ADAPTER_PATH = (
    ROOT
    / "integrations"
    / "agent-skill"
    / "texpage-bridge"
    / "scripts"
    / "texpage_agent.py"
)
SPEC = importlib.util.spec_from_file_location("texpage_agent_adapter", ADAPTER_PATH)
assert SPEC and SPEC.loader
adapter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(adapter)


class AgentAdapterTests(unittest.TestCase):
    def fake_home(self, root: Path) -> Path:
        home = root / "bridge"
        home.mkdir()
        (home / "texpage_bridge.py").write_text("# test stub\n", encoding="utf-8")
        return home.resolve()

    def test_status_delegates_without_loading_private_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = self.fake_home(root)
            # A private-looking file may exist, but the adapter does not need to open it.
            (home / "projects.json").write_text("DO_NOT_READ", encoding="utf-8")
            with mock.patch.object(
                adapter.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=0),
            ) as run_mock:
                rc = adapter.main(
                    ["--bridge-home", str(home), "sample-project", "status"]
                )

            self.assertEqual(rc, 0)
            run_mock.assert_called_once_with(
                [
                    adapter.sys.executable,
                    str(home / "texpage_bridge.py"),
                    "sample-project",
                    "status",
                ],
                cwd=home,
                check=False,
            )

    def test_build_forwards_only_safe_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = self.fake_home(Path(tmp))
            with mock.patch.object(
                adapter.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=7),
            ) as run_mock:
                rc = adapter.main(
                    [
                        "--bridge-home",
                        str(home),
                        "sample-project",
                        "build",
                        "--timeout",
                        "90",
                        "--no-push",
                    ]
                )

            self.assertEqual(rc, 7)
            command = run_mock.call_args.args[0]
            self.assertEqual(
                command[2:],
                ["sample-project", "build", "--timeout", "90", "--no-push"],
            )
            self.assertNotIn("publish", command)
            self.assertNotIn("broker", command)

    def test_submit_and_request_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = self.fake_home(Path(tmp))
            with mock.patch.object(
                adapter.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=0),
            ) as run_mock:
                self.assertEqual(
                    adapter.main(
                        ["--bridge-home", str(home), "demo", "submit", "--timeout", "30"]
                    ),
                    0,
                )
                submit_command = run_mock.call_args.args[0]
                self.assertEqual(
                    submit_command[2:], ["demo", "submit", "--timeout", "30"]
                )

                self.assertEqual(
                    adapter.main(
                        [
                            "--bridge-home",
                            str(home),
                            "demo",
                            "request",
                            "tp-20260820-120000-deadbeef",
                        ]
                    ),
                    0,
                )
                request_command = run_mock.call_args.args[0]
                self.assertEqual(
                    request_command[2:],
                    ["demo", "request", "tp-20260820-120000-deadbeef"],
                )

    def test_environment_bridge_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = self.fake_home(Path(tmp))
            with mock.patch.dict(os.environ, {"TEXPAGE_BRIDGE_HOME": str(home)}), mock.patch.object(
                adapter.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=0),
            ) as run_mock:
                self.assertEqual(adapter.main(["demo", "requests", "--limit", "5"]), 0)
            self.assertEqual(
                run_mock.call_args.args[0][2:], ["demo", "requests", "--limit", "5"]
            )

    def test_admin_commands_are_not_parseable(self) -> None:
        for command in ("publish", "versions", "reserve-version", "set-version", "broker"):
            with self.subTest(command=command), self.assertRaises(SystemExit):
                adapter.main(["demo", command])

    def test_invalid_alias_and_out_of_range_arguments_are_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            adapter.main(["../projects.json", "status"])
        with self.assertRaises(SystemExit):
            adapter.main(["demo", "build", "--timeout", "0"])
        with self.assertRaises(SystemExit):
            adapter.main(["demo", "requests", "--limit", "101"])

    def test_missing_explicit_bridge_home_fails_without_subprocess(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            adapter.subprocess, "run"
        ) as run_mock:
            rc = adapter.main(
                ["--bridge-home", str(Path(tmp) / "missing"), "demo", "status"]
            )
        self.assertEqual(rc, 3)
        run_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
