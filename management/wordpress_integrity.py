#!/usr/local/lib/Ocean3inaBox/env/bin/python3
"""Track filesystem and managed database changes in WordPress installations."""

import argparse
import difflib
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

from system_backup import MARIADB_DUMP, managed_wordpress_databases
from utils import load_environment


STATE_FILENAME = "wordpress-integrity.json"
MAX_TEXT_SNAPSHOT_BYTES = 8192
MAX_TEXT_STATE_BYTES = 256 * 1024
MAX_DETAIL_FILE_BYTES = 2048
MAX_DETAIL_BYTES = 6000


def state_path(env):
	return Path(env["STORAGE_ROOT"]) / STATE_FILENAME


def file_digest(filename):
	digest = hashlib.sha256()
	with filename.open("rb") as f:
		for block in iter(lambda: f.read(1024 * 1024), b""):
			digest.update(block)
	return digest.hexdigest()


def inventory_site(root):
	files = {}
	for directory, dirnames, filenames in os.walk(root):
		dirnames[:] = [name for name in dirnames if not os.path.islink(os.path.join(directory, name))]
		for filename in filenames:
			path = Path(directory, filename)
			if not path.is_file() or path.is_symlink():
				continue
			files[str(path.relative_to(root))] = file_digest(path)
	return files


def managed_sites(env):
	return [config.parent for config in Path(env["STORAGE_ROOT"], "www").glob("*/wp-config.php")]


def compare_inventory(previous, current):
	changes = []
	for filename in sorted(set(previous) | set(current)):
		if filename not in previous:
			changes.append({"type": "added", "file": filename})
		elif filename not in current:
			changes.append({"type": "removed", "file": filename})
		elif previous[filename] != current[filename]:
			changes.append({"type": "modified", "file": filename})
	return changes


def _is_sensitive_path(relative_path):
	parts = Path(relative_path).parts
	name = Path(relative_path).name.casefold()
	return name == "wp-config.php" or (any(part.casefold() == "civicrm" for part in parts) and any(word in name for word in ("settings", "config", "secret")))


def _safe_text_snapshot(path, relative_path):
	"""Return a small UTF-8 text baseline, never including known secret settings."""
	if _is_sensitive_path(relative_path):
		return None
	try:
		if path.stat().st_size > MAX_TEXT_SNAPSHOT_BYTES:
			return None
		content = path.read_bytes()
	except OSError:
		return None
	if b"\0" in content:
		return None
	try:
		return content.decode("utf-8")
	except UnicodeDecodeError:
		return None


def text_snapshots(sites, inventories):
	"""Keep bounded, text-only baselines so Telegram can show safe short diffs."""
	snapshots, remaining = {}, MAX_TEXT_STATE_BYTES
	for site in sorted(sites, key=lambda item: item.name):
		site_snapshots = {}
		for relative_path in sorted(inventories[site.name]):
			content = _safe_text_snapshot(site / relative_path, relative_path)
			if content is None:
				continue
			size = len(content.encode("utf-8"))
			if size > remaining:
				continue
			site_snapshots[relative_path] = content
			remaining -= size
		if site_snapshots:
			snapshots[site.name] = site_snapshots
	return snapshots


def database_fingerprint(database):
	"""Hash a deterministic logical dump without exposing any credentials."""
	if not os.path.exists(MARIADB_DUMP):
		return {"status": "error", "error": "mariadb-dump is not installed"}
	try:
		process = subprocess.Popen(
			[MARIADB_DUMP, "--protocol=socket", "--single-transaction", "--skip-comments", "--skip-dump-date", "--routines", "--events", "--triggers", "--no-create-db", "--skip-add-drop-database", database],
			stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
		)
	except OSError as error:
		return {"status": "error", "error": f"could not start mariadb-dump: {error.strerror}"}
	digest = hashlib.sha256()
	try:
		for block in iter(lambda: process.stdout.read(1024 * 1024), b""):
			digest.update(block)
		returncode = process.wait(timeout=300)
	except subprocess.TimeoutExpired:
		process.kill()
		process.wait()
		return {"status": "error", "error": "mariadb-dump timed out"}
	finally:
		process.stdout.close()
	if returncode:
		return {"status": "error", "error": "mariadb-dump failed"}
	return {"status": "ok", "sha256": digest.hexdigest()}


def database_fingerprints(env):
	"""Fingerprint only databases derived from managed WordPress/CiviCRM configs."""
	return {database: database_fingerprint(database) for database in sorted(managed_wordpress_databases(env["STORAGE_ROOT"]))}


def compare_databases(previous, current):
	changes = []
	for database in sorted(set(previous) | set(current)):
		if database not in current:
			changes.append({"type": "database-removed", "database": database})
		elif current[database].get("status") != "ok":
			changes.append({"type": "database-error", "database": database, "error": current[database].get("error", "unknown dump error")})
		elif database not in previous:
			changes.append({"type": "database-added", "database": database})
		elif previous[database].get("status") != "ok":
			changes.append({"type": "database-error-resolved", "database": database})
		elif previous[database].get("sha256") != current[database].get("sha256"):
			changes.append({"type": "database-modified", "database": database})
	return changes


def load_state(env):
	filename = state_path(env)
	if not filename.exists():
		return {"sites": {}, "databases": {}, "text_snapshots": {}, "changes": [], "initialized": False}
	with filename.open(encoding="utf-8") as f:
		state = json.load(f)
	state.setdefault("sites", {})
	state.setdefault("databases", {})
	state.setdefault("text_snapshots", {})
	state.setdefault("changes", [])
	state.setdefault("initialized", False)
	return state


def save_state(env, state):
	filename = state_path(env)
	with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=filename.parent, delete=False) as f:
		json.dump(state, f, indent=2, sort_keys=True)
		f.write("\n")
		f.flush()
		os.fchmod(f.fileno(), 0o600)
		os.fsync(f.fileno())
	os.replace(f.name, filename)


def scan(env):
	previous = load_state(env)
	sites = managed_sites(env)
	current_sites = {site.name: inventory_site(site) for site in sites}
	current_databases = database_fingerprints(env)
	changes = []
	if previous["initialized"]:
		for site in sorted(set(previous["sites"]) | set(current_sites)):
			if site not in previous["sites"]:
				changes.append({"site": site, "type": "site-added", "file": ""})
			elif site not in current_sites:
				changes.append({"site": site, "type": "site-removed", "file": ""})
			else:
				changes.extend({"site": site, **change} for change in compare_inventory(previous["sites"][site], current_sites[site]))
		changes.extend(compare_databases(previous["databases"], current_databases))
	else:
		changes.extend(
			{"type": "database-error", "database": database, "error": fingerprint.get("error", "unknown dump error")}
			for database, fingerprint in current_databases.items() if fingerprint.get("status") != "ok"
		)
	state = {
		"initialized": True,
		"sites": current_sites,
		"previous_sites": previous["sites"],
		"databases": current_databases,
		"previous_databases": previous["databases"],
		"text_snapshots": text_snapshots(sites, current_sites),
		"previous_text_snapshots": previous["text_snapshots"],
		"changes": changes,
	}
	save_state(env, state)
	return changes


def format_changes(env):
	state = load_state(env)
	if not state["initialized"]:
		return "WordPress integrity scan has not run yet."
	if not state["changes"]:
		return "No WordPress filesystem or managed database changes since the last daily integrity scan."
	lines = ["WordPress integrity changes since the last daily scan:"]
	for change in state["changes"]:
		if "database" in change:
			lines.append(f"{change['type']}: {change['database']}" + (f" ({change['error']})" if change.get("error") else ""))
		else:
			lines.append(f"{change['site']}: {change['type']} {change['file']}".rstrip())
	return "\n".join(lines)


def format_details(env):
	"""Return the latest filenames and bounded, redacted unified text diffs."""
	state = load_state(env)
	if not state["changes"]:
		return format_changes(env)
	lines, used = ["WordPress integrity details:"], 0
	for change in state["changes"]:
		if "database" in change:
			lines.append(f"{change['type']}: {change['database']}" + (f" ({change['error']})" if change.get("error") else ""))
			continue
		label = f"{change['site']}: {change['type']} {change['file']}".rstrip()
		lines.append(label)
		if change["type"] != "modified" or used >= MAX_DETAIL_BYTES:
			continue
		if _is_sensitive_path(change["file"]):
			continue
		old = state.get("previous_text_snapshots", {}).get(change["site"], {}).get(change["file"])
		new = state.get("text_snapshots", {}).get(change["site"], {}).get(change["file"])
		if old is None or new is None:
			continue
		diff = "".join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True), f"{change['file']} (previous)", f"{change['file']} (current)"))
		encoded = diff.encode("utf-8")
		if len(encoded) > MAX_DETAIL_FILE_BYTES:
			diff = encoded[:MAX_DETAIL_FILE_BYTES].decode("utf-8", errors="ignore") + "\n[Diff truncated.]\n"
		if used + len(diff.encode("utf-8")) > MAX_DETAIL_BYTES:
			lines.append("[Further file diffs omitted.]")
			break
		lines.append(diff.rstrip())
		used += len(diff.encode("utf-8"))
	return "\n".join(lines)


def main():
	parser = argparse.ArgumentParser(description="Scan managed WordPress installations for filesystem and database changes.")
	parser.add_argument("command", choices=("scan", "report", "details"))
	args = parser.parse_args()
	env = load_environment()
	if args.command == "scan":
		print(f"Recorded {len(scan(env))} WordPress integrity changes.")
	elif args.command == "details":
		print(format_details(env))
	else:
		print(format_changes(env))


if __name__ == "__main__":
	main()
