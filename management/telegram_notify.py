#!/usr/local/lib/Ocean3inaBox/env/bin/python3
"""Send Telegram reports and process commands from configured recipients."""

import argparse
import datetime
import fcntl
import gzip
import heapq
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from utils import load_environment


DEFAULT_CONFIG = Path("/etc/mailinabox-telegram.conf")
OFFSET_FILE = Path("/var/lib/mailinabox/telegram-offset")
MAX_LOG_BYTES = 1024 * 1024
TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]+$")
CHAT_ID_RE = re.compile(r"^(?:-?\d+|@[A-Za-z0-9_]{5,})$")
NUMERIC_CHAT_ID_RE = re.compile(r"^-?\d+$")
PERMISSIONS = ("logs", "system", "wordpress", "daily_report")
LOG_FILES = {
	"nginx-access": Path("/var/log/nginx/access.log"),
	"nginx-error": Path("/var/log/nginx/error.log"),
	"mail": Path("/var/log/mail.log"),
	"fail2ban": Path("/var/log/fail2ban.log"),
}


def validate_config(token, chat_id):
	if not TOKEN_RE.fullmatch(token or ""):
		raise ValueError("The Telegram bot token is invalid.")
	if not CHAT_ID_RE.fullmatch(chat_id or ""):
		raise ValueError("The Telegram chat ID is invalid.")


def validate_recipient_chat_id(chat_id):
	if not NUMERIC_CHAT_ID_RE.fullmatch(str(chat_id or "")):
		raise ValueError("Telegram recipient chat IDs must be numeric.")
	return str(chat_id)


def _parse_config(filename):
	if not filename.exists():
		raise ValueError(f"Telegram configuration does not exist: {filename}")
	if not filename.is_file():
		raise ValueError("Telegram configuration is not a regular file.")
	if os.stat(filename).st_mode & 0o077:
		raise ValueError(f"{filename} must not be accessible to group or other users (use chmod 600).")
	values = {}
	for line in filename.read_text(encoding="utf-8").splitlines():
		if "=" in line:
			key, value = line.split("=", 1)
			values[key] = value
	validate_config(values.get("TELEGRAM_BOT_TOKEN"), values.get("TELEGRAM_CHAT_ID"))
	return values


def _normalise_recipient(recipient, owner_chat_id=None):
	if not isinstance(recipient, dict):
		raise ValueError("Telegram recipient records must be objects.")
	chat_id = str(recipient.get("chat_id", ""))
	is_owner = chat_id == str(owner_chat_id)
	if is_owner:
		if not CHAT_ID_RE.fullmatch(chat_id):
			raise ValueError("The Telegram owner chat ID is invalid.")
	else:
		validate_recipient_chat_id(chat_id)
	label = recipient.get("label", "")
	if not isinstance(label, str):
		raise ValueError("The Telegram recipient label must be text.")
	label = label.strip()
	if len(label) > 128:
		raise ValueError("The Telegram recipient label must be at most 128 characters.")
	if not label:
		label = "Owner" if is_owner else chat_id
	permissions = recipient.get("permissions", {})
	if not isinstance(permissions, dict):
		raise ValueError("Telegram recipient permissions must be an object.")
	if any(not isinstance(permissions.get(permission, False), bool) for permission in PERMISSIONS):
		raise ValueError("Telegram recipient permissions must be true or false.")
	normalised = {
		"chat_id": chat_id,
		"label": label,
		"owner": is_owner,
		"permissions": {permission: permissions.get(permission, False) for permission in PERMISSIONS},
	}
	if is_owner:
		normalised["permissions"] = {permission: True for permission in PERMISSIONS}
	return normalised


def load_telegram_config(filename=DEFAULT_CONFIG):
	"""Load credentials and public recipient records, migrating legacy config in memory."""
	values = _parse_config(Path(filename))
	try:
		serialized_recipients = values.get("TELEGRAM_RECIPIENTS", "[]")
		recipients = json.loads(serialized_recipients)
	except json.JSONDecodeError as e:
		raise ValueError("Telegram recipient configuration is invalid.") from e
	if not isinstance(recipients, list):
		raise ValueError("Telegram recipient configuration must be a list.")
	owner_chat_id = values["TELEGRAM_CHAT_ID"]
	by_chat_id = {}
	for recipient in recipients:
		normalised = _normalise_recipient(recipient, owner_chat_id)
		if normalised["chat_id"] in by_chat_id:
			raise ValueError("Telegram recipient chat IDs must be unique.")
		by_chat_id[normalised["chat_id"]] = normalised
	# TELEGRAM_CHAT_ID remains the immutable owner record for backwards compatibility.
	owner = by_chat_id.pop(owner_chat_id, None) or {
		"chat_id": owner_chat_id,
		"label": "Owner",
		"permissions": {},
	}
	owner = _normalise_recipient(owner, owner_chat_id)
	return {
		"token": values["TELEGRAM_BOT_TOKEN"],
		"owner_chat_id": owner_chat_id,
		"recipients": [owner, *sorted(by_chat_id.values(), key=lambda recipient: (recipient["label"].casefold(), recipient["chat_id"]))],
	}


def load_config(filename=DEFAULT_CONFIG):
	"""Compatibility helper returning the bot token and original owner chat ID."""
	config = load_telegram_config(filename)
	return config["token"], config["owner_chat_id"]


def public_recipients(config):
	"""Return recipient data that is safe to return from the management API."""
	return [
		{
			"chat_id": recipient["chat_id"],
			"label": recipient["label"],
			"owner": recipient["owner"],
			"permissions": recipient["permissions"].copy(),
		}
		for recipient in config["recipients"]
	]


def write_telegram_config(filename, token, owner_chat_id, recipients):
	validate_config(token, owner_chat_id)
	normalised = []
	seen = set()
	for recipient in recipients:
		item = _normalise_recipient(recipient, owner_chat_id)
		if item["chat_id"] in seen:
			raise ValueError("Telegram recipient chat IDs must be unique.")
		seen.add(item["chat_id"])
		normalised.append(item)
	if owner_chat_id not in seen:
		normalised.append(_normalise_recipient({"chat_id": owner_chat_id, "label": "Owner", "permissions": {}}, owner_chat_id))
	normalised.sort(key=lambda recipient: (not recipient["owner"], recipient["label"].casefold(), recipient["chat_id"]))
	filename = Path(filename)
	filename.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
	temporary_name = None
	try:
		with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=filename.parent, delete=False) as f:
			temporary_name = f.name
			f.write(f"TELEGRAM_BOT_TOKEN={token}\n")
			f.write(f"TELEGRAM_CHAT_ID={owner_chat_id}\n")
			f.write("TELEGRAM_RECIPIENTS=" + json.dumps(normalised, separators=(",", ":"), sort_keys=True) + "\n")
			f.flush()
			os.fchmod(f.fileno(), 0o600)
			os.fsync(f.fileno())
		os.replace(temporary_name, filename)
		temporary_name = None
		os.chmod(filename, 0o600)
		directory_fd = os.open(filename.parent, os.O_DIRECTORY)
		try:
			os.fsync(directory_fd)
		finally:
			os.close(directory_fd)
	finally:
		if temporary_name:
			os.unlink(temporary_name)


def write_config(filename, token, chat_id):
	"""Configure a bot and its original, all-permissions owner."""
	write_telegram_config(filename, token, chat_id, [])


def update_recipient(filename, chat_id, label, permissions):
	config = load_telegram_config(filename)
	chat_id = str(chat_id)
	found = False
	recipients = []
	for recipient in config["recipients"]:
		if recipient["chat_id"] == chat_id:
			found = True
			recipients.append({"chat_id": chat_id, "label": label, "permissions": permissions})
		else:
			recipients.append(recipient)
	if not found:
		raise ValueError("Telegram recipient does not exist.")
	write_telegram_config(filename, config["token"], config["owner_chat_id"], recipients)
	return public_recipients(load_telegram_config(filename))


def add_recipient(filename, chat_id, label, permissions):
	config = load_telegram_config(filename)
	chat_id = validate_recipient_chat_id(chat_id)
	if any(recipient["chat_id"] == chat_id for recipient in config["recipients"]):
		raise ValueError("That Telegram chat ID is already configured.")
	config["recipients"].append({"chat_id": chat_id, "label": label, "permissions": permissions})
	write_telegram_config(filename, config["token"], config["owner_chat_id"], config["recipients"])
	return public_recipients(load_telegram_config(filename))


def delete_recipient(filename, chat_id):
	config = load_telegram_config(filename)
	chat_id = str(chat_id)
	if chat_id == config["owner_chat_id"]:
		raise ValueError("The original Telegram owner cannot be removed.")
	recipients = [recipient for recipient in config["recipients"] if recipient["chat_id"] != chat_id]
	if len(recipients) == len(config["recipients"]):
		raise ValueError("Telegram recipient does not exist.")
	write_telegram_config(filename, config["token"], config["owner_chat_id"], recipients)
	return public_recipients(load_telegram_config(filename))


def run_wp(site_root, arguments):
	wp_cli = "/usr/local/bin/wp" if os.path.exists("/usr/local/bin/wp") else "/usr/local/bin/wp-cli"
	result = subprocess.run(
		["/usr/sbin/runuser", "-u", "www-data", "--", wp_cli, *arguments, "--format=json", "--skip-plugins", "--skip-themes"],
		cwd=site_root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False, timeout=120,
	)
	if result.returncode != 0:
		raise RuntimeError(f"Could not check WordPress site {site_root}: {result.stderr.strip()}")
	return json.loads(result.stdout)


def wordpress_updates(env):
	if env.get("INSTALL_WORDPRESS") != "1":
		return []
	sites = []
	for config in Path(env["STORAGE_ROOT"], "www").glob("*/wp-config.php"):
		site_root = str(config.parent)
		core = run_wp(site_root, ["core", "check-update"])
		plugins = run_wp(site_root, ["plugin", "list", "--update=available"])
		themes = run_wp(site_root, ["theme", "list", "--update=available"])
		sites.append((config.parent.name, core, plugins, themes))
	return sites


def installed_system_updates():
	report_dates = {datetime.date.today().isoformat(), (datetime.date.today() - datetime.timedelta(days=1)).isoformat()}
	updates = []
	for filename in (Path("/var/log/dpkg.log"), Path("/var/log/dpkg.log.1.gz")):
		if not filename.exists():
			continue
		opener = gzip.open if filename.suffix == ".gz" else open
		with opener(filename, "rt", encoding="utf-8", errors="replace") as f:
			for line in f:
				if line[:10] in report_dates and " status installed " in line:
					updates.append(line.strip().split(" status installed ", 1)[1])
	return sorted(set(updates))


def build_report(env):
	lines = ["Daily server update report", ""]
	sites = wordpress_updates(env)
	if not sites:
		lines.append("WordPress: no managed WordPress sites.")
	else:
		for site, core, plugins, themes in sites:
			items = []
			items.extend(f"WordPress {item.get('version')} available" for item in core)
			items.extend(f"plugin {item.get('name')} {item.get('update_version')}" for item in plugins)
			items.extend(f"theme {item.get('name')} {item.get('update_version')}" for item in themes)
			lines.append(f"WordPress ({site}): " + (", ".join(items) if items else "up to date."))
	updates = installed_system_updates()
	lines.append("System updates installed today: " + (", ".join(updates) if updates else "none."))
	return "\n".join(lines)


def split_message(text, limit=4096):
	chunks, chunk = [], ""
	for line in text.splitlines(keepends=True):
		if len(line) > limit:
			raise ValueError("A Telegram report line exceeds the message size limit.")
		if len(chunk) + len(line) > limit:
			chunks.append(chunk)
			chunk = ""
		chunk += line
	if chunk:
		chunks.append(chunk)
	return chunks


def send_telegram_message(token, chat_id, text):
	for chunk in split_message(text):
		payload = json.dumps({"chat_id": chat_id, "text": chunk}).encode("utf-8")
		request = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=payload, headers={"Content-Type": "application/json"}, method="POST")
		with urllib.request.urlopen(request, timeout=30) as response:
			result = json.loads(response.read())
		if not result.get("ok"):
			raise RuntimeError("Telegram rejected the notification.")


def telegram_request(token, method, payload=None):
	data = json.dumps(payload).encode("utf-8") if payload is not None else None
	request = urllib.request.Request(
		f"https://api.telegram.org/bot{token}/{method}", data=data,
		headers={"Content-Type": "application/json"} if data is not None else {}, method="POST" if data is not None else "GET",
	)
	with urllib.request.urlopen(request, timeout=35) as response:
		result = json.loads(response.read())
	if not result.get("ok"):
		raise RuntimeError(f"Telegram rejected {method}.")
	return result["result"]


def read_log(source, mode, pattern=None):
	if source in {"mailinabox", "system"}:
		command = ["journalctl", "--no-pager", "--output=short-iso"]
		command.extend(["-u", "mailinabox"] if source == "mailinabox" else ["-p", "warning..alert"])
		if mode == "tail":
			command.extend(["-n", "100"])
		output = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False, timeout=30)
		if output.returncode != 0:
			raise RuntimeError(output.stderr.strip() or f"Could not read {source} logs.")
		text = output.stdout
	else:
		path = LOG_FILES.get(source)
		if path is None:
			raise ValueError("Unknown log source.")
		if not path.is_file():
			raise ValueError(f"The {source} log is not available.")
		with path.open(encoding="utf-8", errors="replace") as f:
			text = "".join(f.readlines()[-100:]) if mode == "tail" else f.read(MAX_LOG_BYTES + 1)
	if mode == "grep":
		if not pattern:
			raise ValueError("Provide text to search for after grep.")
		text = "\n".join(line for line in text.splitlines() if pattern.casefold() in line.casefold())
	if len(text.encode("utf-8")) > MAX_LOG_BYTES:
		text = text.encode("utf-8")[:MAX_LOG_BYTES].decode("utf-8", errors="ignore") + "\n[Output truncated at 1 MiB.]"
	return text or "[No matching log entries.]"


def command_output(command, timeout=30):
	result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False, timeout=timeout)
	if result.returncode != 0:
		raise RuntimeError(result.stderr.strip() or "System command failed.")
	return result.stdout.strip() or "[No output.]"


def system_summary():
	hostname = command_output(["hostname", "--fqdn"])
	uptime = command_output(["uptime", "--pretty"])
	memory = command_output(["free", "-h"])
	disk = command_output(["df", "-h", "-x", "tmpfs", "-x", "devtmpfs"])
	return f"Hostname: {hostname}\nUptime: {uptime}\n\nMemory:\n{memory}\n\nDisk:\n{disk}"


def largest_directories():
	output = command_output(["du", "-x", "-B1", "--max-depth=3", "/"], timeout=120)
	entries = []
	for line in output.splitlines():
		try:
			size, path = line.split("\t", 1)
			entries.append((int(size), path))
		except ValueError:
			continue
	return "\n".join(f"{size / 1024 / 1024:.1f} MiB\t{path}" for size, path in heapq.nlargest(20, entries)) or "[No directories found.]"


def largest_files():
	process = subprocess.Popen(["find", "/", "-xdev", "-type", "f", "-printf", "%s\t%p\n"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
	entries, deadline = [], time.monotonic() + 120
	try:
		for line in process.stdout:
			if time.monotonic() > deadline:
				raise subprocess.TimeoutExpired(process.args, 120)
			try:
				size, path = line.rstrip("\n").split("\t", 1)
				entry = (int(size), path)
				if len(entries) < 20:
					heapq.heappush(entries, entry)
				elif entry > entries[0]:
					heapq.heapreplace(entries, entry)
			except ValueError:
				continue
		if process.wait() != 0:
			raise RuntimeError("Could not scan system files.")
	finally:
		if process.poll() is None:
			process.kill()
			process.wait()
	return "\n".join(f"{size / 1024 / 1024:.1f} MiB\t{path}" for size, path in sorted(entries, reverse=True)) or "[No files found.]"


def system_command(command, argument=None):
	if command == "/system":
		return system_summary()
	if command == "/users":
		return command_output(["who"])
	if command == "/ports":
		return command_output(["ss", "-lntup"])
	if command == "/largest":
		if argument in {None, "directories", "dirs"}:
			return largest_directories()
		if argument == "files":
			return largest_files()
		raise ValueError("Use /largest directories or /largest files.")
	raise ValueError("Unknown system command.")


def help_message(recipient):
	permissions = recipient["permissions"]
	sections = []
	if permissions["logs"]:
		sections.append("Log commands:\n/log <mailinabox|nginx-access|nginx-error|mail|fail2ban|system> [tail|full|grep <text>]")
	if permissions["system"]:
		sections.append("System commands:\n/system\n/users\n/ports\n/largest [directories|files]")
	if permissions["wordpress"]:
		sections.append("WordPress:\n/wordpress-changes\n/wordpress-details (latest changed files and safe, short text diffs)")
	if not sections:
		return "No bot commands are enabled for this chat."
	return "\n\n".join(sections) + "\n\nDefault log mode is tail (last 100 lines). Full output is limited to 1 MiB."


def handle_bot_command(token, recipient, text):
	# Keep direct callers of the original chat-ID API working; polling always passes
	# a configured recipient record.
	if isinstance(recipient, str):
		recipient = _normalise_recipient({"chat_id": recipient, "label": "Owner", "permissions": {}}, recipient)
	parts = text.strip().split(maxsplit=3)
	if not parts:
		return
	command = parts[0].split("@", 1)[0].lower()
	if command in {"/start", "/help"}:
		send_telegram_message(token, recipient["chat_id"], help_message(recipient))
		return
	if command in {"/logs", "/log"}:
		required_permission = "logs"
	elif command in {"/system", "/users", "/ports", "/largest"}:
		required_permission = "system"
	elif command in {"/wordpress-changes", "/wordpress-details"}:
		required_permission = "wordpress"
	else:
		send_telegram_message(token, recipient["chat_id"], "Unknown command. Send /help for usage.")
		return
	if not recipient["permissions"].get(required_permission, False):
		send_telegram_message(token, recipient["chat_id"], "You are not authorized to use that command.")
		return
	if command == "/logs":
		send_telegram_message(token, recipient["chat_id"], help_message(recipient))
		return
	if command == "/wordpress-changes":
		from wordpress_integrity import format_changes
		send_telegram_message(token, recipient["chat_id"], format_changes(load_environment()))
		return
	if command == "/wordpress-details":
		from wordpress_integrity import format_details
		send_telegram_message(token, recipient["chat_id"], format_details(load_environment()))
		return
	if command in {"/system", "/users", "/ports", "/largest"}:
		try:
			send_telegram_message(token, recipient["chat_id"], system_command(command, parts[1].lower() if len(parts) > 1 else None))
		except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as e:
			send_telegram_message(token, recipient["chat_id"], f"System request failed: {e}")
		return
	if len(parts) < 2:
		send_telegram_message(token, recipient["chat_id"], "Usage: /log <source> [tail|full|grep <text>]")
		return
	source, mode = parts[1], parts[2].lower() if len(parts) > 2 else "tail"
	pattern = parts[3] if len(parts) > 3 else None
	if mode not in {"tail", "full", "grep"}:
		send_telegram_message(token, recipient["chat_id"], "Mode must be tail, full, or grep.")
		return
	try:
		send_telegram_message(token, recipient["chat_id"], f"{source} ({mode}):\n{read_log(source, mode, pattern)}")
	except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as e:
		send_telegram_message(token, recipient["chat_id"], f"Log request failed: {e}")


def poll_commands(token, recipients, offset_file=OFFSET_FILE):
	"""Poll only exact, numeric configured chat IDs; channel owners cannot issue commands."""
	if isinstance(recipients, str):
		recipients = [_normalise_recipient({"chat_id": recipients, "label": "Owner", "permissions": {}}, recipients)]
	authorized = {recipient["chat_id"]: recipient for recipient in recipients if NUMERIC_CHAT_ID_RE.fullmatch(recipient["chat_id"])}
	offset_file = Path(offset_file)
	offset_file.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
	with offset_file.open("a+", encoding="utf-8") as state:
		fcntl.flock(state, fcntl.LOCK_EX | fcntl.LOCK_NB)
		state.seek(0)
		offset = state.read().strip()
		updates = telegram_request(token, "getUpdates", {"offset": int(offset) if offset else None, "timeout": 25, "allowed_updates": ["message"]})
		for update in updates:
			next_offset = update["update_id"] + 1
			message = update.get("message", {})
			recipient = authorized.get(str(message.get("chat", {}).get("id")))
			if recipient and isinstance(message.get("text"), str):
				handle_bot_command(token, recipient, message["text"])
			state.seek(0)
			state.truncate()
			state.write(str(next_offset))
			state.flush()
			os.fchmod(state.fileno(), 0o600)


def send_daily_report(token, recipients, report):
	for recipient in recipients:
		if recipient["permissions"]["daily_report"]:
			send_telegram_message(token, recipient["chat_id"], report)


def send_wordpress_change_prompt(token, recipients, env):
	"""Offer the latest integrity detail only to recipients allowed to request it."""
	from wordpress_integrity import load_state
	changes = load_state(env)["changes"]
	if not changes:
		return
	message = f"WordPress integrity scan detected {len(changes)} change(s). Reply /wordpress-details for the latest changed files and safe details."
	for recipient in recipients:
		if recipient["permissions"]["wordpress"]:
			send_telegram_message(token, recipient["chat_id"], message)


def main():
	parser = argparse.ArgumentParser(description="Configure or send Mail-in-a-Box Telegram update reports.")
	parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
	subparsers = parser.add_subparsers(dest="command", required=True)
	configure = subparsers.add_parser("configure", help="Store Telegram credentials in a mode-0600 config file.")
	configure.add_argument("--token", required=True)
	configure.add_argument("--chat-id", required=True)
	report = subparsers.add_parser("send-report", help="Send the daily WordPress and system update report.")
	report.add_argument("--token")
	report.add_argument("--chat-id")
	wordpress_changes = subparsers.add_parser("send-wordpress-changes", help="Notify WordPress-authorized recipients about the latest integrity changes.")
	wordpress_changes.add_argument("--token")
	wordpress_changes.add_argument("--chat-id")
	subparsers.add_parser("poll", help="Receive authorized Telegram bot commands.")
	args = parser.parse_args()
	if os.geteuid() != 0:
		parser.error("This command must be run as root.")
	try:
		if args.command == "configure":
			write_config(args.config, args.token, args.chat_id)
			print(f"Telegram configuration saved to {args.config}.")
			return 0
		token_option, chat_id_option = getattr(args, "token", None), getattr(args, "chat_id", None)
		if bool(token_option) != bool(chat_id_option):
			raise ValueError("--token and --chat-id must be provided together.")
		if token_option:
			validate_config(token_option, chat_id_option)
			if args.command == "poll":
				raise ValueError("Polling requires configured recipients.")
			if args.command == "send-wordpress-changes":
				send_wordpress_change_prompt(token_option, [{"chat_id": chat_id_option, "permissions": {"wordpress": True}}], load_environment())
				return 0
			send_telegram_message(token_option, chat_id_option, build_report(load_environment()))
			return 0
		config = load_telegram_config(args.config)
		if args.command == "poll":
			poll_commands(config["token"], config["recipients"])
		elif args.command == "send-wordpress-changes":
			send_wordpress_change_prompt(config["token"], config["recipients"], load_environment())
		else:
			send_daily_report(config["token"], config["recipients"], build_report(load_environment()))
		return 0
	except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
		print(f"Telegram notification failed: {e}", file=sys.stderr)
		return 1


if __name__ == "__main__":
	sys.exit(main())
