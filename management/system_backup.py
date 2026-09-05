"""Create and restore the deliberately small system portion of a backup."""

import json
import os
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import urllib.parse


SYSTEM_BACKUP_DIRECTORY = "system-backup"
MANIFEST_NAME = "manifest.json"
MARIADB = "/usr/bin/mariadb"
MARIADB_DUMP = "/usr/bin/mariadb-dump"
SERVICE = "/usr/sbin/service"
UFW = "/usr/sbin/ufw"

# These service settings are restored by default. Host settings intentionally
# live in a separate manifest section and require an explicit restore opt-in.
SYSTEM_SOURCES = (
	("/etc/mailinabox-telegram.conf", "config/mailinabox-telegram.conf", "file"),
	("/var/lib/mailinabox/telegram-offset", "state/telegram-offset", "file"),
	("/etc/nginx", "config/nginx", "directory"),
	("/etc/postfix", "config/postfix", "directory"),
	("/etc/dovecot", "config/dovecot", "directory"),
	("/etc/fail2ban", "config/fail2ban", "directory"),
	("/etc/ufw", "config/ufw", "directory"),
)

# Do not add /etc directories here. This list deliberately excludes SSH host
# and user keys, including root's authorized_keys: restoring configuration must
# not replace credentials or unexpectedly grant access.
HOST_SOURCES = (
	("/etc/hostname", "host/hostname", "file"),
	("/etc/hosts", "host/hosts", "file"),
	("/etc/netplan", "host/netplan", "directory"),
	("/etc/network/interfaces", "host/network/interfaces", "file"),
	("/etc/network/interfaces.d", "host/network/interfaces.d", "directory"),
	("/etc/ssh/sshd_config", "host/ssh/sshd_config", "file"),
	("/etc/ssh/sshd_config.d", "host/ssh/sshd_config.d", "directory"),
)

DATABASE_NAME_RE = re.compile(r"^(?:wp|civi)_[A-Za-z0-9_]{1,60}$")
DATABASE_USER_RE = re.compile(r"^[A-Za-z0-9_]{1,60}$")
PHP_DEFINE_RE = re.compile(
	r"""^\s*define\(\s*['"](?P<name>DB_NAME|DB_USER|DB_PASSWORD)['"]\s*,\s*'(?P<value>(?:\\.|[^'])*)'\s*\)\s*;""",
	re.MULTILINE,
)
CIVICRM_DSN_RE = re.compile(r"""define\(\s*['"]CIVICRM_DSN['"]\s*,\s*['"](?P<dsn>(?:\\.|[^'])*)['"]\s*\)""")


def _php_single_quoted_value(value):
	"""Decode the two escapes accepted by a PHP single-quoted string."""
	return value.replace(r"\\", "\\").replace(r"\'", "'")


def managed_wordpress_databases(storage_root):
	"""Return managed WordPress databases found in wp-config.php files.

	The database name must appear in a site configuration and have the managed
	``wp_`` prefix. This avoids dumping unrelated MariaDB databases.
	"""
	www_root = os.path.join(storage_root, "www")
	databases = {}
	try:
		entries = sorted(os.scandir(www_root), key=lambda entry: entry.name)
	except FileNotFoundError:
		return databases

	for entry in entries:
		if not entry.is_dir(follow_symlinks=False):
			continue
		config = os.path.join(entry.path, "wp-config.php")
		try:
			with open(config, encoding="utf-8") as f:
				content = f.read(1024 * 1024)
		except (FileNotFoundError, OSError, UnicodeDecodeError):
			continue

		values = {match.group("name"): _php_single_quoted_value(match.group("value")) for match in PHP_DEFINE_RE.finditer(content)}
		database = values.get("DB_NAME")
		if database and DATABASE_NAME_RE.fullmatch(database):
			databases[database] = {
				"user": values.get("DB_USER"),
				"password": values.get("DB_PASSWORD"),
			}
		civicrm_settings = os.path.join(entry.path, "wp-content", "uploads", "civicrm", "civicrm.settings.php")
		try:
			with open(civicrm_settings, encoding="utf-8") as f:
				civicrm_content = f.read(1024 * 1024)
			dsn_match = CIVICRM_DSN_RE.search(civicrm_content)
			if dsn_match:
				dsn = urllib.parse.urlsplit(_php_single_quoted_value(dsn_match.group("dsn")))
				civicrm_database = dsn.path.lstrip("/")
				if dsn.scheme == "mysql" and DATABASE_NAME_RE.fullmatch(civicrm_database):
					databases[civicrm_database] = {
						"user": urllib.parse.unquote(dsn.username or ""),
						"password": urllib.parse.unquote(dsn.password or ""),
					}
		except (FileNotFoundError, OSError, UnicodeDecodeError, ValueError):
			pass
	return databases


def _metadata(path):
	info = os.lstat(path)
	if stat.S_ISREG(info.st_mode):
		kind = "file"
	elif stat.S_ISDIR(info.st_mode):
		kind = "directory"
	elif stat.S_ISLNK(info.st_mode):
		kind = "symlink"
	else:
		raise ValueError(f"Cannot snapshot unsupported file type: {path}")
	return {
		"type": kind,
		"mode": stat.S_IMODE(info.st_mode),
		"uid": info.st_uid,
		"gid": info.st_gid,
	}


def _safe_relative_path(path):
	if not isinstance(path, str) or not path or path.startswith("/"):
		return False
	parts = path.split("/")
	return all(part not in ("", ".", "..") for part in parts)


def _symlink_is_within_root(path, target, root):
	if not isinstance(target, str) or target.startswith("/"):
		return False
	resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
	return resolved == root or resolved.startswith(root + "/")


def _copy_regular_file(source, destination):
	flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
	source_fd = os.open(source, flags)
	try:
		destination_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
		try:
			with os.fdopen(source_fd, "rb", closefd=False) as source_file, os.fdopen(destination_fd, "wb", closefd=False) as destination_file:
				shutil.copyfileobj(source_file, destination_file)
				destination_file.flush()
				os.fsync(destination_file.fileno())
		finally:
			os.close(destination_fd)
	finally:
		os.close(source_fd)


def _copy_source_entry(source, destination, relative_path, root, entries):
	metadata = _metadata(source)
	entry = {"path": relative_path, **metadata}
	entries.append(entry)
	if metadata["type"] == "directory":
		os.mkdir(destination, 0o700)
		for child in sorted(os.scandir(source), key=lambda item: item.name):
			_copy_source_entry(child.path, os.path.join(destination, child.name), relative_path + "/" + child.name, root, entries)
	elif metadata["type"] == "file":
		_copy_regular_file(source, destination)
	else:
		target = os.readlink(source)
		if not _symlink_is_within_root(relative_path, target, root):
			raise ValueError(f"Refusing to snapshot unsafe symlink: {source}")
		os.symlink(target, destination)


def _write_json(path, value):
	with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as f:
		json.dump(value, f, sort_keys=True, indent=2)
		f.write("\n")
		f.flush()
		os.fsync(f.fileno())


def _run_dump(database, destination):
	with os.fdopen(os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as output:
		try:
			subprocess.run(
				[MARIADB_DUMP, "--protocol=socket", "--single-transaction", "--routines", "--events", "--triggers", "--no-create-db", "--skip-add-drop-database", database],
				stdout=output,
				stderr=subprocess.DEVNULL,
				check=True,
			)
		except subprocess.CalledProcessError as error:
			raise RuntimeError(f"Could not dump managed MariaDB database {database}.") from error
		output.flush()
		os.fsync(output.fileno())


def _remove_path(path):
	if not os.path.lexists(path):
		return
	if os.path.islink(path) or not os.path.isdir(path):
		os.unlink(path)
	else:
		shutil.rmtree(path)


def create_system_snapshot(storage_root):
	"""Create a restrictive snapshot that duplicity will include in its backup."""
	snapshot = os.path.join(storage_root, SYSTEM_BACKUP_DIRECTORY)
	staging = os.path.join(storage_root, "." + SYSTEM_BACKUP_DIRECTORY + ".new")
	previous = os.path.join(storage_root, "." + SYSTEM_BACKUP_DIRECTORY + ".previous")
	_remove_path(staging)
	_remove_path(previous)
	os.mkdir(staging, 0o700)
	completed = False
	try:
		sources = []
		for source, relative_path, expected_type in SYSTEM_SOURCES + HOST_SOURCES:
			if not os.path.lexists(source):
				continue
			metadata = _metadata(source)
			if metadata["type"] != expected_type:
				raise ValueError(f"Expected {source} to be a {expected_type}.")
			entries = []
			os.makedirs(os.path.dirname(os.path.join(staging, relative_path)), mode=0o700, exist_ok=True)
			_copy_source_entry(source, os.path.join(staging, relative_path), relative_path, relative_path, entries)
			item = {"source": source, "snapshot": relative_path, "type": expected_type, "entries": entries}
			if (source, relative_path, expected_type) in HOST_SOURCES:
				item["host_specific"] = True
			sources.append(item)

		databases = managed_wordpress_databases(storage_root)
		dumps = []
		if databases:
			if not os.path.exists(MARIADB_DUMP):
				raise RuntimeError("Managed WordPress databases exist but mariadb-dump is not installed.")
			dump_directory = os.path.join(staging, "databases")
			os.mkdir(dump_directory, 0o700)
			for database in sorted(databases):
				relative_path = "databases/" + database + ".sql"
				_run_dump(database, os.path.join(staging, relative_path))
				dumps.append({"name": database, "path": relative_path, "mode": 0o600})

		_write_json(os.path.join(staging, MANIFEST_NAME), {"version": 2, "sources": sources, "databases": dumps})
		if os.path.lexists(snapshot):
			os.replace(snapshot, previous)
		os.replace(staging, snapshot)
		completed = True
	finally:
		if completed:
			_remove_path(previous)
		else:
			_remove_path(staging)
			if not os.path.lexists(snapshot) and os.path.lexists(previous):
				os.replace(previous, snapshot)


def _read_manifest(snapshot):
	try:
		if _metadata(snapshot)["type"] != "directory" or _metadata(os.path.join(snapshot, MANIFEST_NAME))["type"] != "file":
			raise ValueError
	except (OSError, ValueError) as error:
		raise ValueError("System backup manifest is missing or invalid.") from error
	try:
		with open(os.path.join(snapshot, MANIFEST_NAME), encoding="utf-8") as f:
			manifest = json.load(f)
	except (OSError, ValueError) as error:
		raise ValueError("System backup manifest is missing or invalid.") from error
	if not isinstance(manifest, dict) or manifest.get("version") not in (1, 2):
		raise ValueError("System backup manifest has an unsupported version.")
	return manifest


def _validate_snapshot_manifest(snapshot, manifest):
	"""Validate all snapshot paths before changing the host."""
	if not isinstance(manifest.get("sources"), list) or not isinstance(manifest.get("databases"), list):
		raise ValueError("System backup manifest is incomplete.")
	expected_sources = {item[1]: (item[0], item[2], False) for item in SYSTEM_SOURCES}
	if manifest["version"] >= 2:
		expected_sources.update({item[1]: (item[0], item[2], True) for item in HOST_SOURCES})
	validated_sources = []
	expected_paths = {MANIFEST_NAME}

	for source in manifest["sources"]:
		if not isinstance(source, dict):
			raise ValueError("System backup manifest contains an invalid source.")
		relative_path = source.get("snapshot")
		if relative_path not in expected_sources or source.get("source") != expected_sources[relative_path][0] or source.get("type") != expected_sources[relative_path][1] or not isinstance(source.get("host_specific", False), bool) or source.get("host_specific", False) != expected_sources[relative_path][2]:
			raise ValueError("System backup manifest contains a non-whitelisted source.")
		if any(item["snapshot"] == relative_path for item in validated_sources):
			raise ValueError("System backup manifest contains a duplicate source.")
		if not isinstance(source.get("entries"), list) or not source["entries"]:
			raise ValueError("System backup manifest contains an empty source.")
		seen = set()
		for entry in source["entries"]:
			if not isinstance(entry, dict) or not _safe_relative_path(entry.get("path")) or entry["path"] in seen:
				raise ValueError("System backup manifest contains an invalid path.")
			path = entry["path"]
			if path != relative_path and not path.startswith(relative_path + "/"):
				raise ValueError("System backup manifest path is outside its source.")
			if entry.get("type") not in ("file", "directory", "symlink") or not all(isinstance(entry.get(key), int) and entry[key] >= 0 for key in ("mode", "uid", "gid")):
				raise ValueError("System backup manifest contains invalid metadata.")
			if entry["mode"] > 0o7777:
				raise ValueError("System backup manifest contains an invalid mode.")
			archive_path = os.path.join(snapshot, *path.split("/"))
			try:
				actual = _metadata(archive_path)
			except (OSError, ValueError) as error:
				raise ValueError("System backup contents do not match its manifest.") from error
			if actual["type"] != entry["type"]:
				raise ValueError("System backup contents do not match its manifest.")
			if entry["type"] == "symlink" and not _symlink_is_within_root(path, os.readlink(archive_path), relative_path):
				raise ValueError("System backup contains an unsafe symlink.")
			seen.add(path)
			expected_paths.add(path)
		if relative_path not in seen:
			raise ValueError("System backup manifest is missing a source root.")
		path_parts = relative_path.split("/")
		for index in range(1, len(path_parts)):
			expected_paths.add("/".join(path_parts[:index]))
		validated_sources.append(source)

	validated_dumps = []
	for dump in manifest["databases"]:
		if not isinstance(dump, dict) or not isinstance(dump.get("name"), str) or not DATABASE_NAME_RE.fullmatch(dump["name"]):
			raise ValueError("System backup manifest contains an unsafe database name.")
		if dump.get("path") != "databases/" + dump["name"] + ".sql" or dump.get("mode") != 0o600:
			raise ValueError("System backup manifest contains an invalid database dump.")
		try:
			if _metadata(os.path.join(snapshot, *dump["path"].split("/")))["type"] != "file":
				raise ValueError
		except (OSError, ValueError) as error:
			raise ValueError("System backup database dump is missing or invalid.") from error
		expected_paths.add("databases")
		expected_paths.add(dump["path"])
		validated_dumps.append(dump)

	actual_paths = set()
	for current_root, directories, files in os.walk(snapshot, followlinks=False):
		for name in directories + files:
			relative_path = os.path.relpath(os.path.join(current_root, name), snapshot).replace(os.sep, "/")
			actual_paths.add(relative_path)
	if actual_paths != expected_paths:
		raise ValueError("System backup contains unmanifested files.")
	return validated_sources, validated_dumps


def _apply_metadata(path, entry):
	if entry["type"] == "symlink":
		os.lchown(path, entry["uid"], entry["gid"])
	else:
		os.chown(path, entry["uid"], entry["gid"])
		os.chmod(path, entry["mode"])


def _restore_source(snapshot, source):
	destination = source["source"]
	relative_root = source["snapshot"]
	_remove_path(destination)
	for entry in sorted(source["entries"], key=lambda item: (item["path"].count("/"), item["path"])):
		relative = entry["path"]
		target = destination + relative.removeprefix(relative_root)
		archive_path = os.path.join(snapshot, *relative.split("/"))
		if entry["type"] == "directory":
			os.mkdir(target, 0o700)
		elif entry["type"] == "file":
			_copy_regular_file(archive_path, target)
		else:
			os.symlink(os.readlink(archive_path), target)
	for entry in sorted(source["entries"], key=lambda item: item["path"].count("/"), reverse=True):
		target = destination + entry["path"].removeprefix(relative_root)
		_apply_metadata(target, entry)


def _sql_literal(value):
	return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def _restore_database(snapshot, dump, credentials):
	database = dump["name"]
	if not os.path.exists(MARIADB):
		raise RuntimeError("MariaDB is required to restore managed WordPress databases.")
	sql = f"DROP DATABASE IF EXISTS `{database}`;\nCREATE DATABASE `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;\n"
	user = credentials.get("user")
	password = credentials.get("password")
	if not isinstance(user, str) or not DATABASE_USER_RE.fullmatch(user) or password is None:
		raise RuntimeError(f"Cannot safely restore MariaDB user for {database}: wp-config.php credentials are incomplete.")
	sql += f"CREATE USER IF NOT EXISTS {_sql_literal(user)}@'localhost' IDENTIFIED BY {_sql_literal(password)};\n"
	sql += f"ALTER USER {_sql_literal(user)}@'localhost' IDENTIFIED BY {_sql_literal(password)};\n"
	sql += f"GRANT ALL PRIVILEGES ON `{database}`.* TO {_sql_literal(user)}@'localhost';\n"
	try:
		subprocess.run([MARIADB, "--protocol=socket"], input=sql.encode("utf-8"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
		with open(os.path.join(snapshot, *dump["path"].split("/")), "rb") as sql_dump:
			subprocess.run([MARIADB, "--protocol=socket", "--database=" + database], stdin=sql_dump, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
	except subprocess.CalledProcessError as error:
		raise RuntimeError(f"Could not restore managed MariaDB database {database}.") from error


def _restored_service_commands(sources):
	restored = {source["source"] for source in sources}
	commands = []
	if "/etc/nginx" in restored:
		commands.append(([SERVICE, "nginx", "reload"], "nginx"))
	if "/etc/postfix" in restored:
		commands.append(([SERVICE, "postfix", "restart"], "postfix"))
	if "/etc/dovecot" in restored:
		commands.append(([SERVICE, "dovecot", "restart"], "dovecot"))
	if "/etc/fail2ban" in restored:
		commands.append(([SERVICE, "fail2ban", "restart"], "fail2ban"))
	if "/etc/ufw" in restored:
		commands.append(([UFW, "reload"], "UFW"))
	if "/etc/ssh/sshd_config" in restored or "/etc/ssh/sshd_config.d" in restored:
		commands.append(([SERVICE, "ssh", "reload"], "SSH"))
	return commands


def _validate_restore_requirements(sources, dumps):
	if dumps and not os.path.exists(MARIADB):
		raise RuntimeError("MariaDB is required to restore managed WordPress databases.")
	for command, name in _restored_service_commands(sources):
		if not os.path.exists(command[0]):
			raise RuntimeError(f"Cannot reload restored {name} configuration: {command[0]} is not installed.")


def _restart_restored_services(sources):
	for command, name in _restored_service_commands(sources):
		try:
			subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
		except subprocess.CalledProcessError as error:
			raise RuntimeError(f"Could not reload restored {name} configuration.") from error


def _validate_host_ssh_config(snapshot, sources):
	"""Validate the archived daemon configuration before replacing live SSH config."""
	if not any(source["source"].startswith("/etc/ssh/") for source in sources):
		return
	sshd = "/usr/sbin/sshd"
	if not os.path.exists(sshd):
		raise RuntimeError("Cannot validate restored SSH configuration: sshd is not installed.")
	config = os.path.join(snapshot, "host", "ssh", "sshd_config")
	if not os.path.exists(config):
		raise RuntimeError("Cannot restore SSH configuration without an archived sshd_config.")
	try:
		subprocess.run([sshd, "-t", "-f", config], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
	except subprocess.CalledProcessError as error:
		raise RuntimeError("Archived SSH configuration did not pass sshd validation.") from error


def host_restore_requested(explicit, stdin=None, stdout=None):
	"""Require terminal confirmation unless a caller explicitly opted in."""
	if explicit:
		return True
	stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
	if not stdin.isatty() or not stdout.isatty():
		return False
	stdout.write("Restore hostname, network, and SSH daemon configuration (never SSH keys)? [y/N] ")
	stdout.flush()
	answer = stdin.readline()
	return answer.strip().lower() in ("y", "yes")


def restore_system_snapshot(restored_storage_root, restore_host_config=False):
	"""Restore a snapshot only after duplicity has restored STORAGE_ROOT."""
	if os.geteuid() != 0:
		raise RuntimeError("System restore must be run as root.")
	snapshot = os.path.join(os.path.abspath(restored_storage_root), SYSTEM_BACKUP_DIRECTORY)
	manifest = _read_manifest(snapshot)
	sources, dumps = _validate_snapshot_manifest(snapshot, manifest)
	service_sources = [source for source in sources if not source.get("host_specific")]
	host_sources = [source for source in sources if source.get("host_specific")]
	_validate_restore_requirements(service_sources, dumps)

	for source in service_sources:
		_restore_source(snapshot, source)
	credentials = managed_wordpress_databases(restored_storage_root)
	for dump in dumps:
		_restore_database(snapshot, dump, credentials.get(dump["name"], {}))
	_restart_restored_services(service_sources)
	if restore_host_config and host_sources:
		_validate_host_ssh_config(snapshot, host_sources)
		_validate_restore_requirements(host_sources, [])
		for source in host_sources:
			_restore_source(snapshot, source)
		_restart_restored_services(host_sources)
