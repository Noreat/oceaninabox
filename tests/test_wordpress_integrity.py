import sys
import unittest
import os
import shutil
import uuid
from unittest.mock import patch

sys.path.insert(0, "management")
import wordpress_integrity


class WordPressIntegrityTests(unittest.TestCase):
	def test_compare_inventory_reports_all_change_types(self):
		self.assertEqual(
			wordpress_integrity.compare_inventory(
				{"kept.php": "one", "removed.php": "two", "changed.php": "old"},
				{"kept.php": "one", "added.php": "three", "changed.php": "new"},
			),
			[
				{"type": "added", "file": "added.php"},
				{"type": "modified", "file": "changed.php"},
				{"type": "removed", "file": "removed.php"},
			],
		)

	def test_database_diff_reports_changes_and_errors(self):
		self.assertEqual(
			wordpress_integrity.compare_databases(
				{"wp_old": {"status": "ok", "sha256": "one"}, "wp_removed": {"status": "ok", "sha256": "two"}},
				{"wp_old": {"status": "ok", "sha256": "three"}, "wp_bad": {"status": "error", "error": "dump failed"}},
			),
			[
				{"type": "database-error", "database": "wp_bad", "error": "dump failed"},
				{"type": "database-modified", "database": "wp_old"},
				{"type": "database-removed", "database": "wp_removed"},
			],
		)

	def test_database_fingerprints_only_uses_managed_databases(self):
		env = {"STORAGE_ROOT": "unused"}
		with patch.object(wordpress_integrity, "managed_wordpress_databases", return_value={"wp_site": {}, "civi_site": {}}), patch.object(wordpress_integrity, "database_fingerprint", side_effect=lambda name: {"status": "ok", "sha256": name}):
			self.assertEqual(
				wordpress_integrity.database_fingerprints(env),
				{"civi_site": {"status": "ok", "sha256": "civi_site"}, "wp_site": {"status": "ok", "sha256": "wp_site"}},
			)

	def test_details_redacts_wp_config_content(self):
		root = os.path.join("tests", ".wordpress-integrity-" + uuid.uuid4().hex)
		os.mkdir(root)
		self.addCleanup(shutil.rmtree, root, ignore_errors=True)
		env = {"STORAGE_ROOT": root}
		wordpress_integrity.save_state(env, {
			"initialized": True, "sites": {}, "databases": {}, "text_snapshots": {"site": {"wp-config.php": "new secret"}},
			"previous_text_snapshots": {"site": {"wp-config.php": "old secret"}},
			"changes": [{"site": "site", "type": "modified", "file": "wp-config.php"}],
		})
		details = wordpress_integrity.format_details(env)
		self.assertIn("wp-config.php", details)
		self.assertNotIn("secret", details)


if __name__ == "__main__":
	unittest.main()
