"""Only unsupported local agent sockets may skip the real GnuPG fixtures."""
import errno
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import test_sign_linux as signing


class AgentSocketTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.env = mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "false"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.factory = mock.patch.object(signing.socket, "socket")
        self.socket = self.factory.start()
        self.addCleanup(self.factory.stop)

    def test_allowed_socket_is_closed_and_removed(self):
        probe = self.socket.return_value.__enter__.return_value
        probe.bind.side_effect = lambda path: Path(path).touch()
        signing.require_agent_socket(self.home)
        self.socket.assert_called_once_with(signing.socket.AF_UNIX, signing.socket.SOCK_STREAM)
        probe.bind.assert_called_once_with(str(self.home / "agent-socket-check"))
        self.socket.return_value.__exit__.assert_called_once()
        self.assertEqual(list(self.home.iterdir()), [])

    def test_denied_local_socket_is_an_explicit_skip(self):
        for code in (errno.EPERM, errno.EACCES):
            with self.subTest(errno=code):
                self.socket.side_effect = PermissionError(code, os.strerror(code))
                with self.assertRaisesRegex(unittest.SkipTest, "GnuPG agent needs Unix sockets"):
                    signing.require_agent_socket(self.home)

    def test_denied_bind_is_an_explicit_local_skip(self):
        self.socket.return_value.__enter__.return_value.bind.side_effect = PermissionError(errno.EPERM, "denied")
        with self.assertRaises(unittest.SkipTest):
            signing.require_agent_socket(self.home)
        self.socket.return_value.__exit__.assert_called_once()

    def test_denied_hosted_socket_remains_a_failure(self):
        self.socket.side_effect = PermissionError(errno.EPERM, "denied")
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            with self.assertRaises(PermissionError):
                signing.require_agent_socket(self.home)

    def test_resource_errors_remain_failures(self):
        self.socket.side_effect = OSError(errno.EMFILE, "too many open files")
        with self.assertRaises(OSError) as result:
            signing.require_agent_socket(self.home)
        self.assertEqual(result.exception.errno, errno.EMFILE)


if __name__ == "__main__":
    unittest.main()
