import sys
import unittest

sys.path.insert(0, "management")
import plesk_import


class PleskImportParsingTests(unittest.TestCase):
	def test_extract_email_addresses_deduplicates_and_normalizes(self):
		self.assertEqual(
			plesk_import.extract_email_addresses("one@example.com\nONE@example.com\ninvalid@"),
			["one@example.com"],
		)

	def test_parse_plesk_aliases_reads_alias_value_only(self):
		self.assertEqual(
			plesk_import.parse_plesk_aliases("Mailbox: true\nAliases: sales@example.com, Info@example.com\n"),
			["sales@example.com", "info@example.com"],
		)

	def test_parse_plesk_aliases_requires_alias_heading(self):
		self.assertEqual(plesk_import.parse_plesk_aliases("Email: user@example.com\n"), [])

	def test_get_imap_mailboxes_excludes_non_selectable_folders(self):
		class Client:
			def list(self):
				return "OK", [b'(\\Noselect) "/" "Archive"', b'(\\HasNoChildren) "/" "INBOX"']

		self.assertEqual(plesk_import.get_imap_mailboxes(Client()), ["INBOX"])

	def test_validate_ssh_target_rejects_unsafe_values(self):
		with self.assertRaises(ValueError):
			plesk_import.validate_ssh_target("source.example.com;command", "root")

	def test_validate_ssh_target_accepts_hostname_and_root_user(self):
		plesk_import.validate_ssh_target("plesk.example.com", "root")


if __name__ == "__main__":
	unittest.main()
