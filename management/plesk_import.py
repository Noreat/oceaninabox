#!/usr/local/lib/Ocean3inaBox/env/bin/python3
"""Import Plesk mail accounts, aliases, and messages into Mail-in-a-Box.

The source Plesk host is queried through SSH using its ``plesk bin mail``
command. Source IMAP passwords are read from a mode-0600 CSV file with the
columns ``email,password``. Plesk does not expose existing passwords through
its API or CLI.
"""

import argparse
import csv
import imaplib
import os
import re
import secrets
import stat
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path


EMAIL_RE = re.compile(r"(?<![\w.+-])([a-z0-9_.-]+@[a-z0-9.-]+)(?![\w.+-])", re.IGNORECASE)
ALIAS_HEADING_RE = re.compile(r"^\s*(?:email\s+)?aliases?\s*:\s*(.*)$", re.IGNORECASE)
SSH_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]*[$]?$", re.IGNORECASE)
HOST_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*$", re.IGNORECASE)


def extract_email_addresses(output: str) -> list[str]:
	"""Return unique addresses in their first-seen order from Plesk CLI output."""
	addresses = []
	for address in EMAIL_RE.findall(output):
		address = address.lower()
		if address not in addresses:
			addresses.append(address)
	return addresses


def parse_plesk_aliases(output: str) -> list[str]:
	"""Extract aliases from the ``Aliases:`` value of ``plesk bin mail --info``."""
	for line in output.splitlines():
		match = ALIAS_HEADING_RE.match(line)
		if match:
			return extract_email_addresses(match.group(1))
	return []


def validate_ssh_target(host: str, user: str) -> None:
	if not HOST_RE.fullmatch(host) or ".." in host or host.startswith(".") or host.endswith("."):
		raise ValueError("The Plesk host must be a hostname or IPv4 address.")
	if not SSH_USER_RE.fullmatch(user):
		raise ValueError("The SSH user contains unsupported characters.")


def read_password_csv(filename: Path) -> dict[str, str]:
	mode = stat.S_IMODE(filename.stat().st_mode)
	if mode & 0o077:
		raise ValueError(f"{filename} must not be accessible to group or other users (use chmod 600).")

	with filename.open(encoding="utf-8", newline="") as f:
		reader = csv.DictReader(f)
		if reader.fieldnames != ["email", "password"]:
			raise ValueError("The credentials CSV must have exactly the columns: email,password.")
		passwords = {}
		for row_number, row in enumerate(reader, start=2):
			email = (row["email"] or "").strip().lower()
			password = row["password"] or ""
			if not email or not password:
				raise ValueError(f"Credentials CSV row {row_number} must include an email and password.")
			if email in passwords:
				raise ValueError(f"Credentials CSV contains {email} more than once.")
			passwords[email] = password
	return passwords


def run_ssh(host: str, user: str, identity_file: Path | None, command: list[str]) -> str:
	ssh_command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20"]
	if identity_file is not None:
		ssh_command.extend(["-i", str(identity_file)])
	ssh_command.extend([f"{user}@{host}", *command])
	result = subprocess.run(
		ssh_command,
		check=False,
		stdout=subprocess.PIPE,
		stderr=subprocess.PIPE,
		text=True,
		timeout=60,
	)
	if result.returncode != 0:
		raise RuntimeError(f"Plesk command failed: {result.stderr.strip() or 'SSH connection failed.'}")
	return result.stdout


def get_plesk_accounts(host: str, user: str, identity_file: Path | None) -> list[str]:
	accounts = extract_email_addresses(run_ssh(host, user, identity_file, ["plesk", "bin", "mail", "--list"]))
	if not accounts:
		raise RuntimeError("Plesk did not return any mail accounts.")
	return accounts


def get_plesk_aliases(host: str, user: str, identity_file: Path | None, accounts: Iterable[str]) -> dict[str, list[str]]:
	aliases = {}
	for account in accounts:
		account_aliases = parse_plesk_aliases(run_ssh(host, user, identity_file, ["plesk", "bin", "mail", "--info", account]))
		for alias in account_aliases:
			aliases.setdefault(alias, []).append(account)
	return aliases


def generate_password() -> str:
	# A URL-safe value has sufficient entropy while meeting Mail-in-a-Box password rules.
	return secrets.token_urlsafe(24)


def write_destination_passwords(filename: Path, passwords: dict[str, str]) -> None:
	flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
	fd = os.open(filename, flags, 0o600)
	with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
		writer = csv.writer(f)
		writer.writerow(["email", "password"])
		writer.writerows(sorted(passwords.items()))


def get_imap_mailboxes(client: imaplib.IMAP4_SSL) -> list[str]:
	status, mailboxes = client.list()
	if status != "OK" or mailboxes is None:
		raise RuntimeError("Could not list source IMAP folders.")

	folders = []
	for mailbox in mailboxes:
		if b"\\Noselect" in mailbox.partition(b")")[0]:
			continue
		match = re.search(rb'\) (?:NIL|"[^"]*") (.+)$', mailbox)
		if match is None:
			raise RuntimeError("The source IMAP server returned an unsupported folder name.")
		name = match.group(1)
		if name.startswith(b'"') and name.endswith(b'"'):
			name = name[1:-1].replace(b'\\"', b'"').replace(b"\\\\", b"\\")
		folders.append(name.decode("ascii"))
	return folders


def sync_mailbox(source_host: str, destination_host: str, email: str, source_password: str, destination_password: str) -> None:
	with imaplib.IMAP4_SSL(source_host, timeout=60) as source, imaplib.IMAP4_SSL(destination_host, timeout=60) as destination:
		source.login(email, source_password)
		destination.login(email, destination_password)
		for folder in get_imap_mailboxes(source):
			status, _data = destination.create(folder)
			if status not in {"OK", "NO"}:
				raise RuntimeError(f"Could not create destination folder {folder} for {email}.")
			status, _data = source.select(folder, readonly=True)
			if status != "OK":
				raise RuntimeError(f"Could not open source folder {folder} for {email}.")
			status, message_ids = source.search(None, "ALL")
			if status != "OK":
				raise RuntimeError(f"Could not list messages in {folder} for {email}.")
			for message_id in message_ids[0].split():
				status, message_data = source.fetch(message_id, "(FLAGS INTERNALDATE RFC822)")
				if status != "OK" or not isinstance(message_data[0], tuple):
					raise RuntimeError(f"Could not fetch a message in {folder} for {email}.")
				metadata, message = message_data[0]
				flags = imaplib.ParseFlags(metadata)
				date_match = re.search(rb'INTERNALDATE ("[^"]+")', metadata)
				if date_match is None:
					raise RuntimeError(f"The source IMAP server did not return a message date for {email}.")
				status, _data = destination.append(folder, flags, date_match.group(1).decode("ascii"), message)
				if status != "OK":
					raise RuntimeError(f"Could not import a message into {folder} for {email}.")
		source.logout()
		destination.logout()


def main() -> int:
	parser = argparse.ArgumentParser(
		description="Import Plesk mail accounts, aliases, and messages over SSH and IMAP.",
		epilog="The credentials CSV must be mode 0600 and contain email,password columns. "
		"Destination passwords are written once to --destination-passwords with mode 0600.",
	)
	parser.add_argument("--source-host", required=True, help="Plesk hostname used for SSH and IMAPS.")
	parser.add_argument("--ssh-user", default="root", help="SSH user permitted to execute Plesk CLI commands.")
	parser.add_argument("--ssh-identity", type=Path, help="Mode-0600 private SSH key for the Plesk host.")
	parser.add_argument("--credentials", type=Path, required=True, help="Mode-0600 CSV of source IMAP credentials.")
	parser.add_argument("--destination-passwords", type=Path, help="New mode-0600 CSV for generated destination passwords.")
	parser.add_argument("--dry-run", action="store_true", help="Validate discovery and credentials without changing this box.")
	args = parser.parse_args()

	try:
		validate_ssh_target(args.source_host, args.ssh_user)
		if os.geteuid() != 0:
			raise ValueError("This command must be run as root.")
		if not args.dry_run and args.destination_passwords is None:
			raise ValueError("--destination-passwords is required unless --dry-run is used.")
		if args.destination_passwords is not None and args.destination_passwords.exists():
			raise ValueError(f"{args.destination_passwords} already exists; refusing to overwrite it.")
		if args.ssh_identity is not None:
			if not args.ssh_identity.is_file():
				raise ValueError(f"{args.ssh_identity} is not a readable SSH private key file.")
			if stat.S_IMODE(args.ssh_identity.stat().st_mode) & 0o077:
				raise ValueError(f"{args.ssh_identity} must not be accessible to group or other users (use chmod 600).")

		source_passwords = read_password_csv(args.credentials)
		accounts = get_plesk_accounts(args.source_host, args.ssh_user, args.ssh_identity)
		missing_passwords = sorted(set(accounts) - set(source_passwords))
		if missing_passwords:
			raise ValueError("No source password was provided for: " + ", ".join(missing_passwords))
		unknown_passwords = sorted(set(source_passwords) - set(accounts))
		if unknown_passwords:
			raise ValueError("The credentials CSV contains non-Plesk accounts: " + ", ".join(unknown_passwords))
		aliases = get_plesk_aliases(args.source_host, args.ssh_user, args.ssh_identity, accounts)
		print(f"Discovered {len(accounts)} accounts and {len(aliases)} aliases.")
		if args.dry_run:
			return 0

		import utils
		from mailconfig import add_mail_alias, add_mail_user, get_mail_aliases, get_mail_users

		env = utils.load_environment()
		existing_addresses = set(get_mail_users(env)) | {alias[0] for alias in get_mail_aliases(env)}
		conflicts = sorted((set(accounts) | set(aliases)) & existing_addresses)
		if conflicts:
			raise ValueError("The destination already has these addresses: " + ", ".join(conflicts))

		destination_passwords = {email: generate_password() for email in accounts}
		for email in accounts:
			result = add_mail_user(email, destination_passwords[email], "", "0", env)
			if isinstance(result, tuple):
				raise RuntimeError(result[0])
		for alias, destinations in aliases.items():
			result = add_mail_alias(alias, ",".join(destinations), "", env)
			if isinstance(result, tuple):
				raise RuntimeError(result[0])

		write_destination_passwords(args.destination_passwords, destination_passwords)
		for email in accounts:
			sync_mailbox(args.source_host, env["PRIMARY_HOSTNAME"], email, source_passwords[email], destination_passwords[email])
		print(f"Imported {len(accounts)} accounts and {len(aliases)} aliases. "
			f"New passwords were written to {args.destination_passwords}.")
		return 0
	except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as e:
		print(f"Import failed: {e}", file=sys.stderr)
		return 1


if __name__ == "__main__":
	sys.exit(main())
