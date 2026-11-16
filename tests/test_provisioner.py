import importlib
import logging
import os
from pathlib import Path
import signal
import struct
import sys
import tempfile
import unittest
from unittest import mock
from urllib.error import URLError


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
try:
    provisioner = importlib.import_module("provisioner")
finally:
    sys.path.pop(0)
    for _sig, _handler in _handlers.items():
        signal.signal(_sig, _handler)


class ProvisionerSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = mock.patch.dict(os.environ, {
            "HOME": str(self.root),
            "OCI_A1_STATE_DIR": str(self.root / "state"),
            "OCI_A1_LOG_FILE": str(self.root / "provisioner.log"),
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def private_file(self, name, content):
        path = self.root / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o600)
        return path

    def test_private_file_rejects_group_or_world_permissions_and_directory(self):
        path = self.private_file("secret", "synthetic-secret")
        for mode in (0o640, 0o604, 0o610, 0o601):
            with self.subTest(mode=oct(mode)):
                path.chmod(mode)
                with self.assertRaises(ValueError) as error:
                    provisioner.require_private_file(str(path))
                self.assertNotIn("synthetic-secret", str(error.exception))
        with self.assertRaises(ValueError):
            provisioner.require_private_file(str(self.root))

    def test_named_user_acl_accepts_only_root_and_service_user(self):
        path = self.private_file("credential", "synthetic-secret")
        undefined_id = 0xffffffff

        def set_acl(named_entry):
            entries = [(0x01, 0o6, undefined_id), (0x04, 0, undefined_id)]
            entries.append(named_entry)
            entries.extend([(0x10, 0o4, undefined_id), (0x20, 0, undefined_id)])
            entries.sort(key=lambda entry: entry[0])
            acl = struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *entry) for entry in entries)
            os.setxattr(path, "system.posix_acl_access", acl)

        set_acl((0x02, 0o4, os.geteuid()))
        self.assertEqual(provisioner.require_private_file(path), str(path))

        foreign_uid = 65534 if os.geteuid() != 65534 else 65533
        for entry in ((0x02, 0o4, foreign_uid), (0x08, 0o4, os.getegid())):
            with self.subTest(entry=entry):
                set_acl(entry)
                with self.assertRaises(ValueError):
                    provisioner.require_private_file(path)

    def test_systemd_config_profile_redirects_missing_original_key(self):
        original_key = self.root / "missing-original-key.pem"
        key = self.private_file("oci-api-key", "synthetic key content\n")
        self.private_file("oci-config", (
            "[DEFAULT]\n"
            "region=us-phoenix-1\n"
            "[CUSTOM]\n"
            "user=ocid1.user.oc1..aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
            "tenancy=ocid1.tenancy.oc1..aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
            "fingerprint=aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99\n"
            f"key_file={original_key}\n"
            "region=us-ashburn-1\n"
            "pass_phrase=synthetic-passphrase\n"
        ))
        self.assertFalse(original_key.exists())
        with mock.patch.dict(os.environ, {
            "CREDENTIALS_DIRECTORY": str(self.root),
            "OCI_CONFIG_PROFILE": "CUSTOM",
        }):
            loaded = provisioner.load_oci_config()
        self.assertEqual(loaded["region"], "us-ashburn-1")
        self.assertEqual(loaded["key_file"], str(key))
        self.assertEqual(loaded["pass_phrase"], "synthetic-passphrase")

    def test_inline_webhook_environment_is_ignored(self):
        missing = self.root / "missing-webhook"
        with mock.patch.dict(os.environ, {
            "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/123/synthetic-token",
            "DISCORD_WEBHOOK_FILE": str(missing),
        }), mock.patch.object(provisioner.urllib.request, "urlopen") as urlopen:
            self.assertEqual(provisioner.get_discord_webhook_url(), "")
            self.assertFalse(provisioner.post_discord({"content": "test"}, max_attempts=1))
            urlopen.assert_not_called()

    def test_secure_webhook_read_and_insecure_file_rejected(self):
        url = "https://discord.com/api/webhooks/123/synthetic-token"
        path = self.private_file("webhook", url + "\n")
        with mock.patch.dict(os.environ, {"DISCORD_WEBHOOK_FILE": str(path)}):
            self.assertEqual(provisioner.get_discord_webhook_url(), url)
            path.chmod(0o644)
            with self.assertRaises(ValueError) as error:
                provisioner.get_discord_webhook_url()
            self.assertNotIn(url, str(error.exception))
            self.assertNotIn("synthetic-token", str(error.exception))
            with mock.patch.object(provisioner.urllib.request, "urlopen") as urlopen, \
                    self.assertLogs(level=logging.ERROR) as captured:
                self.assertFalse(provisioner.post_discord({"content": "test"}, max_attempts=1))
            urlopen.assert_not_called()
            self.assertNotIn(url, "\n".join(captured.output))
            self.assertNotIn("synthetic-token", "\n".join(captured.output))

    def test_transport_failure_never_logs_webhook_url_or_token(self):
        token = "synthetic-sensitive-token"
        url = f"https://discord.com/api/webhooks/123/{token}"
        path = self.private_file("webhook", url + "\n")
        with mock.patch.dict(os.environ, {"DISCORD_WEBHOOK_FILE": str(path)}), \
                mock.patch.object(provisioner.urllib.request, "urlopen", side_effect=URLError(url)), \
                self.assertLogs(level=logging.WARNING) as captured:
            self.assertFalse(provisioner.post_discord({"content": "test"}, max_attempts=1))
        output = "\n".join(captured.output)
        self.assertNotIn(url, output)
        self.assertNotIn(token, output)

    def test_invalid_webhook_endpoint_never_posts(self):
        for url in (
            "http://discord.com/api/webhooks/123/synthetic-token",
            "https://example.com/api/webhooks/123/synthetic-token",
            "https://discord.com.example.com/api/webhooks/123/synthetic-token",
            "https://discord.com/not-a-webhook/123/synthetic-token",
        ):
            with self.subTest(url=url):
                path = self.private_file("webhook", url + "\n")
                with mock.patch.dict(os.environ, {"DISCORD_WEBHOOK_FILE": str(path)}), \
                        mock.patch.object(provisioner.urllib.request, "urlopen") as urlopen:
                    with self.assertRaises(ValueError) as error:
                        provisioner.get_discord_webhook_url()
                    self.assertNotIn(url, str(error.exception))
                    self.assertNotIn("synthetic-token", str(error.exception))
                    with self.assertLogs(level=logging.ERROR) as captured:
                        self.assertFalse(provisioner.post_discord({"content": "test"}, max_attempts=1))
                    self.assertNotIn(url, "\n".join(captured.output))
                    self.assertNotIn("synthetic-token", "\n".join(captured.output))
                    urlopen.assert_not_called()

    def test_retry_delay_bounds_and_throttle_cap(self):
        expected = {
            0: (90, 120),
            1: (130, 165),
            2: (250, 285),
            3: (490, 525),
            4: (970, 1005),
            100: (970, 1005),
        }
        for count, (lower, upper) in expected.items():
            for endpoint, value in ((0, lower), (1, upper)):
                with self.subTest(count=count, endpoint=endpoint), \
                        mock.patch.object(provisioner.random, "uniform", side_effect=lambda a, b: (a, b)[endpoint]) as uniform:
                    self.assertEqual(provisioner.retry_delay(count), value)
                    uniform.assert_called_once_with(*((0, 30) if count == 0 else (10, 45)))

if __name__ == "__main__":
    unittest.main()
