import sys
import unittest

sys.path.insert(0, "management")
import wordpress


class WordPressHelperTests(unittest.TestCase):
	def test_database_identifiers_are_stable_and_safe(self):
		identifiers = wordpress.wordpress_database_identifiers("example.com")
		self.assertEqual(identifiers, wordpress.wordpress_database_identifiers("example.com"))
		self.assertRegex(identifiers["database"], r"^wp_[0-9a-f]{20}$")
		self.assertEqual(identifiers["database"], identifiers["user"])

	def test_domain_root_uses_safe_domain_filename(self):
		env = {"STORAGE_ROOT": "/home/user-data"}
		self.assertEqual(
			wordpress.wordpress_root("a/b.example", env),
			"/home/user-data/www/a%2Fb.example",
		)

	def test_nginx_config_checks_scripts_before_fastcgi(self):
		config = wordpress.wordpress_nginx_config()
		self.assertIn("try_files $uri $uri/ /index.php?$args;", config)
		self.assertIn("try_files $uri =404;", config)
		self.assertIn("fastcgi_param SCRIPT_FILENAME $document_root$fastcgi_script_name;", config)
		self.assertIn("wp-config", config)
		self.assertIn("wp-content/uploads/.*\\.php", config)
		self.assertLess(config.index("try_files $uri =404;"), config.index("fastcgi_pass php-fpm;"))

	def test_generated_config_escapes_php_literals(self):
		config = wordpress.wordpress_config("db", "user", "pa'ss\\word")
		self.assertIn("define('DB_PASSWORD', 'pa\\'ss\\\\word');", config)
		self.assertIn("define('DB_CHARSET', 'utf8mb4');", config)
		self.assertNotIn("pa'ss\\word", config)


if __name__ == "__main__":
	unittest.main()
