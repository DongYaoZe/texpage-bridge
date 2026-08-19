from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import texpage_bridge as bridge


class BrokerEndpointTests(unittest.TestCase):
    def test_defaults(self) -> None:
        self.assertEqual(
            bridge.broker_endpoint({}),
            (bridge.DEFAULT_BROKER_HOST, bridge.DEFAULT_BROKER_PORT),
        )

    def test_overrides(self) -> None:
        self.assertEqual(
            bridge.broker_endpoint({"broker_host": "127.0.0.2", "broker_port": "45000"}),
            ("127.0.0.2", 45000),
        )


class SnapshotPushRetryTests(unittest.TestCase):
    def test_retries_only_known_ref_materialization_race(self) -> None:
        transient = bridge.BridgeError(
            "Command failed (1): git push\n"
            "remote: error: cannot lock ref 'refs/heads/v1.0': reference already exists"
        )
        with mock.patch.object(bridge, "run", side_effect=[transient, ""]) as run_mock, mock.patch.object(
            bridge.time, "sleep"
        ) as sleep_mock:
            bridge.push_snapshot(Path("."), "texpage", "v1.0", "deadbeef")

        self.assertEqual(run_mock.call_count, 2)
        sleep_mock.assert_called_once_with(0.75)

    def test_does_not_retry_unrelated_git_failure(self) -> None:
        failure = bridge.BridgeError("Command failed (1): git push\nfatal: authentication failed")
        with mock.patch.object(bridge, "run", side_effect=failure) as run_mock, mock.patch.object(
            bridge.time, "sleep"
        ) as sleep_mock:
            with self.assertRaises(bridge.BridgeError):
                bridge.push_snapshot(Path("."), "texpage", "v1.0", "deadbeef")

        self.assertEqual(run_mock.call_count, 1)
        sleep_mock.assert_not_called()


class LocalCacheTests(unittest.TestCase):
    def test_cache_exclude_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".git" / "info").mkdir(parents=True)

            first = bridge.ensure_local_cache(repo)
            second = bridge.ensure_local_cache(repo)

            self.assertEqual(first, second)
            self.assertTrue(first.is_dir())
            exclude = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
            self.assertEqual(exclude.count(".texpage/"), 1)


if __name__ == "__main__":
    unittest.main()
