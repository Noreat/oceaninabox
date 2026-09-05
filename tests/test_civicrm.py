import sys
import unittest

sys.path.insert(0, "management")
import civicrm


class CiviCRMTests(unittest.TestCase):
	def test_civicrm_is_controlled_by_setup_setting(self):
		self.assertTrue(civicrm.civicrm_enabled({"INSTALL_CIVICRM": "1"}))
		self.assertFalse(civicrm.civicrm_enabled({"INSTALL_CIVICRM": "0"}))

	def test_plugin_path_is_domain_specific(self):
		env = {"STORAGE_ROOT": "/home/user-data"}
		self.assertEqual(
			civicrm.civicrm_plugin_path("example.com", env),
			"/home/user-data/www/example.com/wp-content/plugins/civicrm/civicrm.php",
		)


if __name__ == "__main__":
	unittest.main()
