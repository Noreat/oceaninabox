import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, "management")
import telegram_notify


class TelegramConfigTests(unittest.TestCase):
	def setUp(self):
		self.config = Path(f"tests/.telegram-config-{os.getpid()}-{id(self)}")
		self.offset = Path(f"tests/.telegram-offset-{os.getpid()}-{id(self)}")

	def tearDown(self):
		for path in (self.config, self.offset):
			path.unlink(missing_ok=True)

	def test_validates_bot_token_and_numeric_chat_id(self):
		telegram_notify.validate_config("123456:AbCd_ef-gh", "-1001234567890")

	def test_validates_bot_token_and_channel_chat_id(self):
		telegram_notify.validate_config("123456:AbCd_ef-gh", "@mailinabox")

	def test_rejects_invalid_credentials(self):
		with self.assertRaises(ValueError):
			telegram_notify.validate_config("not-a-token", "bad chat")

	def test_splits_messages_at_line_boundaries(self):
		self.assertEqual(telegram_notify.split_message("one\ntwo\n", limit=5), ["one\n", "two\n"])

	def test_log_command_accepts_only_supported_modes(self):
		with self.assertRaises(ValueError):
			telegram_notify.read_log("unsupported", "tail")

	def test_largest_command_rejects_unknown_kind(self):
		with self.assertRaises(ValueError):
			telegram_notify.system_command("/largest", "unknown")

	def test_legacy_config_migrates_owner_with_all_permissions(self):
		self.config.write_text("TELEGRAM_BOT_TOKEN=123456:AbCd_ef-gh\nTELEGRAM_CHAT_ID=@mailinabox\n", encoding="utf-8")
		self.config.chmod(0o600)

		config = telegram_notify.load_telegram_config(self.config)

		self.assertEqual(config["recipients"][0]["chat_id"], "@mailinabox")
		self.assertTrue(all(config["recipients"][0]["permissions"].values()))
		self.assertNotIn("token", telegram_notify.public_recipients(config)[0])

	def test_recipient_config_is_private_and_rejects_channel_recipients(self):
		telegram_notify.write_config(self.config, "123456:AbCd_ef-gh", "-1001234567890")
		telegram_notify.add_recipient(
			self.config, "998877", "On-call", {"logs": True, "system": False, "wordpress": False, "daily_report": True}
		)

		config = telegram_notify.load_telegram_config(self.config)
		self.assertEqual(os.stat(self.config).st_mode & 0o777, 0o600)
		self.assertEqual(config["recipients"][1]["label"], "On-call")
		self.assertTrue(config["recipients"][1]["permissions"]["logs"])
		with self.assertRaises(ValueError):
			telegram_notify.add_recipient(self.config, "@channel", "Channel", {})

	def test_owner_cannot_be_deleted_or_lose_permissions(self):
		telegram_notify.write_config(self.config, "123456:AbCd_ef-gh", "1234")
		with self.assertRaises(ValueError):
			telegram_notify.delete_recipient(self.config, "1234")

		telegram_notify.update_recipient(self.config, "1234", "Primary owner", {})
		owner = telegram_notify.load_telegram_config(self.config)["recipients"][0]
		self.assertEqual(owner["label"], "Primary owner")
		self.assertTrue(all(owner["permissions"].values()))

	def test_poll_does_not_handle_unconfigured_chats(self):
		recipients = [{
			"chat_id": "1234",
			"label": "On-call",
			"owner": False,
			"permissions": {"logs": True, "system": False, "wordpress": False, "daily_report": False},
		}]
		updates = [
			{"update_id": 1, "message": {"chat": {"id": 9999}, "text": "/system"}},
			{"update_id": 2, "message": {"chat": {"id": 1234}, "text": "/help"}},
		]
		with patch.object(telegram_notify, "telegram_request", return_value=updates), patch.object(telegram_notify, "handle_bot_command") as handler:
			telegram_notify.poll_commands("123456:AbCd_ef-gh", recipients, self.offset)

		handler.assert_called_once_with("123456:AbCd_ef-gh", recipients[0], "/help")

	def test_command_permissions_limit_help_and_prevent_handler_execution(self):
		recipient = {
			"chat_id": "1234",
			"label": "Log reader",
			"owner": False,
			"permissions": {"logs": True, "system": False, "wordpress": False, "daily_report": False},
		}
		with patch.object(telegram_notify, "send_telegram_message") as send:
			telegram_notify.handle_bot_command("123456:AbCd_ef-gh", recipient, "/help")
		self.assertIn("Log commands", send.call_args.args[2])
		self.assertNotIn("System commands", send.call_args.args[2])

		with patch.object(telegram_notify, "send_telegram_message") as send, patch.object(telegram_notify, "system_command") as system:
			telegram_notify.handle_bot_command("123456:AbCd_ef-gh", recipient, "/system")
		system.assert_not_called()
		self.assertEqual(send.call_args.args[2], "You are not authorized to use that command.")

	def test_daily_report_only_sends_to_permitted_recipients(self):
		recipients = [
			{"chat_id": "1", "permissions": {"daily_report": True}},
			{"chat_id": "2", "permissions": {"daily_report": False}},
		]
		with patch.object(telegram_notify, "send_telegram_message") as send:
			telegram_notify.send_daily_report("123456:AbCd_ef-gh", recipients, "report")
		send.assert_called_once_with("123456:AbCd_ef-gh", "1", "report")

	def test_wordpress_details_requires_permission(self):
		recipient = {"chat_id": "1234", "label": "Log reader", "owner": False, "permissions": {"logs": True, "system": False, "wordpress": False, "daily_report": False}}
		with patch.object(telegram_notify, "send_telegram_message") as send:
			telegram_notify.handle_bot_command("123456:AbCd_ef-gh", recipient, "/wordpress-details")
		self.assertEqual(send.call_args.args[2], "You are not authorized to use that command.")

	def test_wordpress_change_prompt_only_targets_wordpress_recipients(self):
		recipients = [
			{"chat_id": "1", "permissions": {"wordpress": True}},
			{"chat_id": "2", "permissions": {"wordpress": False}},
		]
		with patch("wordpress_integrity.load_state", return_value={"changes": [{"type": "database-modified", "database": "wp_site"}]}), patch.object(telegram_notify, "send_telegram_message") as send:
			telegram_notify.send_wordpress_change_prompt("123456:AbCd_ef-gh", recipients, {})
		send.assert_called_once()
		self.assertEqual(send.call_args.args[1], "1")
		self.assertIn("/wordpress-details", send.call_args.args[2])


if __name__ == "__main__":
	unittest.main()
