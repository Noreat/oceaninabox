import os
import shutil
import stat
import sys
import unittest
import uuid
import io
from unittest.mock import patch


sys.path.insert(0, "management")
import system_backup


def stat_mode(path):
	return stat.S_IMODE(os.stat(path).st_mode)


class SystemBackupTests(unittest.TestCase):
	def setUp(self):
		self.root = os.path.join(".", "test-system-backup-" + uuid.uuid4().hex)
		os.mkdir(self.root)
		self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

	def _write(self, path, content):
		os.makedirs(os.path.dirname(path), exist_ok=True)
		with open(path, "w", encoding="utf-8") as f:
			f.write(content)

	def test_managed_wordpress_databases_uses_only_managed_configs(self):
		self._write(
			os.path.join(self.root, "www", "site.example", "wp-config.php"),
			"""<?php
define('DB_NAME', 'wp_abc123');
define('DB_USER', 'wp_abc123');
define('DB_PASSWORD', 'it\\'s-secret');
""",
		)
		self._write(
			os.path.join(self.root, "www", "other.example", "wp-config.php"),
			"define('DB_NAME', 'unmanaged_database');",
		)

		self.assertEqual(
			system_backup.managed_wordpress_databases(self.root),
			{"wp_abc123": {"user": "wp_abc123", "password": "it's-secret"}},
		)

	def test_snapshot_copies_metadata_to_restrictive_backup_files(self):
		source = os.path.join(self.root, "source-nginx")
		self._write(os.path.join(source, "nginx.conf"), "events {}")
		os.chmod(source, 0o750)
		os.chmod(os.path.join(source, "nginx.conf"), 0o640)
		original_sources = system_backup.SYSTEM_SOURCES
		system_backup.SYSTEM_SOURCES = ((source, "config/nginx", "directory"),)
		self.addCleanup(setattr, system_backup, "SYSTEM_SOURCES", original_sources)

		system_backup.create_system_snapshot(self.root)

		snapshot = os.path.join(self.root, "system-backup")
		with open(os.path.join(snapshot, "manifest.json"), encoding="utf-8") as f:
			manifest = system_backup.json.load(f)
		self.assertEqual(manifest["sources"][0]["entries"][1]["mode"], 0o640)
		self.assertEqual(stat_mode(snapshot), 0o700)
		self.assertEqual(stat_mode(os.path.join(snapshot, "config", "nginx", "nginx.conf")), 0o600)

	def test_snapshot_manifest_rejects_path_traversal(self):
		snapshot = os.path.join(self.root, "system-backup")
		os.makedirs(snapshot)
		manifest = {
			"version": 1,
			"sources": [{
				"source": "/etc/nginx",
				"snapshot": "config/nginx",
				"type": "directory",
				"entries": [{
					"path": "../etc/passwd",
					"type": "file",
					"mode": 0o600,
					"uid": 0,
					"gid": 0,
				}],
			}],
			"databases": [],
		}

		with self.assertRaisesRegex(ValueError, "invalid path"):
			system_backup._validate_snapshot_manifest(snapshot, manifest)

	def test_snapshot_manifest_accepts_only_whitelisted_sources(self):
		snapshot = os.path.join(self.root, "system-backup")
		os.makedirs(os.path.join(snapshot, "config", "nginx"))
		self._write(os.path.join(snapshot, "config", "nginx", "nginx.conf"), "events {}")
		manifest = {
			"version": 1,
			"sources": [{
				"source": "/etc/ssh",
				"snapshot": "config/nginx",
				"type": "directory",
				"entries": [
					{"path": "config/nginx", "type": "directory", "mode": 0o755, "uid": 0, "gid": 0},
					{"path": "config/nginx/nginx.conf", "type": "file", "mode": 0o644, "uid": 0, "gid": 0},
				],
			}],
			"databases": [],
		}

		with self.assertRaisesRegex(ValueError, "non-whitelisted"):
			system_backup._validate_snapshot_manifest(snapshot, manifest)

	def test_snapshot_manifest_rejects_unsafe_symlink(self):
		snapshot = os.path.join(self.root, "system-backup")
		os.makedirs(os.path.join(snapshot, "config", "nginx"))
		os.symlink("/etc/shadow", os.path.join(snapshot, "config", "nginx", "secret"))
		manifest = {
			"version": 1,
			"sources": [{
				"source": "/etc/nginx",
				"snapshot": "config/nginx",
				"type": "directory",
				"entries": [
					{"path": "config/nginx", "type": "directory", "mode": 0o755, "uid": 0, "gid": 0},
					{"path": "config/nginx/secret", "type": "symlink", "mode": 0o777, "uid": 0, "gid": 0},
				],
			}],
			"databases": [],
		}

		with self.assertRaisesRegex(ValueError, "unsafe symlink"):
			system_backup._validate_snapshot_manifest(snapshot, manifest)

	def test_host_snapshot_is_whitelisted_and_marked_host_specific(self):
		hostname = os.path.join(self.root, "hostname")
		self._write(hostname, "example.test\n")
		original_system, original_host = system_backup.SYSTEM_SOURCES, system_backup.HOST_SOURCES
		system_backup.SYSTEM_SOURCES = ()
		system_backup.HOST_SOURCES = ((hostname, "host/hostname", "file"),)
		self.addCleanup(setattr, system_backup, "SYSTEM_SOURCES", original_system)
		self.addCleanup(setattr, system_backup, "HOST_SOURCES", original_host)

		system_backup.create_system_snapshot(self.root)

		snapshot = os.path.join(self.root, "system-backup")
		manifest = system_backup._read_manifest(snapshot)
		sources, dumps = system_backup._validate_snapshot_manifest(snapshot, manifest)
		self.assertEqual(manifest["version"], 2)
		self.assertTrue(sources[0]["host_specific"])
		self.assertEqual(dumps, [])
		self.assertNotIn("/etc/ssh/ssh_host_rsa_key", [source[0] for source in original_host])

	def test_host_restore_is_excluded_without_explicit_opt_in(self):
		host_source = {"source": "/etc/hostname", "snapshot": "host/hostname", "host_specific": True, "entries": []}
		with patch.object(system_backup.os, "geteuid", return_value=0), patch.object(system_backup, "_read_manifest", return_value={}), patch.object(system_backup, "_validate_snapshot_manifest", return_value=([host_source], [])), patch.object(system_backup, "_validate_restore_requirements") as requirements, patch.object(system_backup, "_restore_source") as restore:
			system_backup.restore_system_snapshot(self.root)
		restore.assert_not_called()
		requirements.assert_called_once_with([], [])

	def test_host_restore_requires_explicit_opt_in_without_a_tty(self):
		self.assertFalse(system_backup.host_restore_requested(False, io.StringIO(), io.StringIO()))
		self.assertTrue(system_backup.host_restore_requested(True, io.StringIO(), io.StringIO()))


if __name__ == "__main__":
	unittest.main()
