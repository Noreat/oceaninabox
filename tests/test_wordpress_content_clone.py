import sys
import unittest

sys.path.insert(0, "management")
import wordpress_content_clone


class WordPressContentCloneTests(unittest.TestCase):
	def test_sanitizer_emits_only_minimal_escaped_text(self):
		content = wordpress_content_clone.sanitize_html(
			'<p onclick="steal()">Hello <strong>world</strong> &amp; '
			'&lt;tag&gt;</p><script>alert(1)</script><iframe>hidden</iframe>'
		)
		self.assertEqual(content, "<p>Hello world &amp; &lt;tag&gt;</p>")
		self.assertNotIn("onclick", content)
		self.assertNotIn("alert", content)
		self.assertNotIn("iframe", content)

	def test_sanitizer_discards_non_content_element_text(self):
		self.assertEqual(
			wordpress_content_clone.sanitize_html("<style>body{display:none}</style><form>Do not copy</form><p>Keep me</p>"),
			"<p>Keep me</p>",
		)

	def test_title_sanitizer_keeps_markup_as_safe_text(self):
		self.assertEqual(
			wordpress_content_clone.sanitize_title('<em>Title</em> &lt;script&gt;'),
			"Title &lt;script&gt;",
		)

	def test_validation_rejects_unsafe_source_values(self):
		with self.assertRaises(ValueError):
			wordpress_content_clone.validate_ssh_target("source.example;id", "root")
		with self.assertRaises(ValueError):
			wordpress_content_clone.validate_source_wordpress_path("/var/www/../wordpress")
		with self.assertRaises(ValueError):
			wordpress_content_clone.validate_destination_domain("Aquatante.com")

	def test_validation_accepts_safe_values_and_fixed_command(self):
		wordpress_content_clone.validate_ssh_target("source.example.com", "root")
		self.assertEqual(wordpress_content_clone.validate_source_wordpress_path("/var/www/wordpress"), "/var/www/wordpress")
		self.assertEqual(wordpress_content_clone.validate_destination_domain("aquatante.com"), "aquatante.com")
		command = wordpress_content_clone.source_wp_command("/var/www/wordpress")
		self.assertEqual(command[:5], ["wp", "--allow-root", "--skip-plugins", "--skip-themes", "--path=/var/www/wordpress"])
		self.assertIn("--format=json", command)


if __name__ == "__main__":
	unittest.main()
