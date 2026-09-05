"""Install and describe WordPress sites managed by the control panel."""

import hashlib
import contextlib
import os
import pwd
import grp
import secrets
import shutil
import stat
import subprocess

from utils import safe_domain_name


MANAGED_NGINX_HEADER = "# Managed by Mail-in-a-Box WordPress. Do not edit.\n"
WP_CLI = "/usr/local/bin/wp"
MARIADB = "/usr/bin/mariadb"
RUNUSER = "/usr/sbin/runuser"


def wordpress_enabled(env):
	return env.get("INSTALL_WORDPRESS") == "1"


def wordpress_root(domain, env):
	return os.path.join(env["STORAGE_ROOT"], "www", safe_domain_name(domain))


def wordpress_nginx_include(domain, env):
	return os.path.join(env["STORAGE_ROOT"], "www", safe_domain_name(domain) + ".conf")


def wordpress_database_identifiers(domain):
	# Domain names can be long or contain IDNA characters. A stable hash gives
	# each site short SQL identifiers without using untrusted text as an identifier.
	digest = hashlib.sha256(domain.encode("utf-8")).hexdigest()[:20]
	return {"database": "wp_" + digest, "user": "wp_" + digest}


def wordpress_nginx_config():
	# This is included inside the already domain-specific server block. Keep the
	# FastCGI block narrow and verify the requested script before executing it.
	return MANAGED_NGINX_HEADER + """\
index index.php index.html index.htm;

location / {
\ttry_files $uri $uri/ /index.php?$args;
}

location ~* ^/(?:wp-config\\.php|wp-config-sample\\.php|readme\\.html|license\\.txt)$ {
\tdeny all;
}

location ~* ^/wp-content/uploads/.*\\.php$ {
\tdeny all;
}

location ~ /\\.(?!well-known/) {
\tdeny all;
}

location ~ \\.php$ {
\ttry_files $uri =404;
\tinclude fastcgi_params;
\tfastcgi_param SCRIPT_FILENAME $document_root$fastcgi_script_name;
\tfastcgi_param SCRIPT_NAME $fastcgi_script_name;
\tfastcgi_pass php-fpm;
}
"""


def _wp_cli_path():
	if os.path.exists(WP_CLI):
		return WP_CLI
	return "/usr/local/bin/wp-cli"


def _php_string(value):
	# Values are generated or validated, but use a PHP single-quoted literal as
	# an additional boundary before writing credentials into wp-config.php.
	return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def wordpress_config(database, database_user, database_password):
	salts = [secrets.token_urlsafe(48) for _ in range(8)]
	salt_names = (
		"AUTH_KEY", "SECURE_AUTH_KEY", "LOGGED_IN_KEY", "NONCE_KEY",
		"AUTH_SALT", "SECURE_AUTH_SALT", "LOGGED_IN_SALT", "NONCE_SALT",
	)
	salt_defines = "\n".join(f"define({_php_string(name)}, {_php_string(value)});" for name, value in zip(salt_names, salts))
	return f"""<?php
define('DB_NAME', {_php_string(database)});
define('DB_USER', {_php_string(database_user)});
define('DB_PASSWORD', {_php_string(database_password)});
define('DB_HOST', 'localhost');
define('DB_CHARSET', 'utf8mb4');
define('DB_COLLATE', '');

{salt_defines}

$table_prefix = 'wp_';
define('WP_DEBUG', false);

if (!defined('ABSPATH')) {{
\tdefine('ABSPATH', __DIR__ . '/');
}}
require_once ABSPATH . 'wp-settings.php';
"""


def _is_empty_directory(path):
	try:
		with os.scandir(path) as entries:
			return next(entries, None) is None
	except FileNotFoundError:
		return True


def _set_www_data_ownership(path):
	account = pwd.getpwnam("www-data")
	group = grp.getgrnam("www-data")
	os.chown(path, account.pw_uid, group.gr_gid)


def _write_exclusive(path, content, mode):
	fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
	try:
		with os.fdopen(fd, "w", encoding="utf-8") as f:
			f.write(content)
			f.flush()
			os.fsync(f.fileno())
	except BaseException:
		with contextlib.suppress(FileNotFoundError):
			os.unlink(path)
		raise


def _sql_literal(value):
	return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def _run_mariadb(sql, capture_output=False):
	return subprocess.run(
		[MARIADB, "--protocol=socket", "--batch", "--skip-column-names"],
		input=sql.encode("utf-8"),
		stdout=subprocess.PIPE if capture_output else subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
		check=True,
	)


def _assert_database_objects_absent(identifiers):
	database = identifiers["database"]
	user = identifiers["user"]
	lookup = (
		"SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = " + _sql_literal(database) + ";\n"
		"SELECT User FROM mysql.user WHERE User = " + _sql_literal(user) + " AND Host = 'localhost';\n"
	)
	if _run_mariadb(lookup, capture_output=True).stdout.strip():
		raise ValueError("A WordPress database or database user already exists for this domain.")


def _create_database(identifiers, password):
	database = identifiers["database"]
	user = identifiers["user"]
	sql = (
		f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;\n"
		f"CREATE USER {_sql_literal(user)}@'localhost' IDENTIFIED BY {_sql_literal(password)};\n"
		f"GRANT ALL PRIVILEGES ON `{database}`.* TO {_sql_literal(user)}@'localhost';\n"
	)
	_run_mariadb(sql)


def _remove_database(identifiers):
	database = identifiers["database"]
	user = identifiers["user"]
	sql = f"DROP USER IF EXISTS {_sql_literal(user)}@'localhost';\nDROP DATABASE IF EXISTS `{database}`;\n"
	with contextlib.suppress(subprocess.CalledProcessError):
		_run_mariadb(sql)


def _run_wp(arguments, stdin=None):
	subprocess.run(
		[RUNUSER, "-u", "www-data", "--", _wp_cli_path(), *arguments],
		input=stdin,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
		check=True,
	)


def _validate_title(title):
	if not isinstance(title, str):
		raise ValueError("A site title is required.")
	title = title.strip()
	if not title or len(title) > 200 or not title.isprintable():
		raise ValueError("The site title must be between 1 and 200 printable characters.")
	return title


def _validate_admin_email(email):
	from mailconfig import validate_email

	if not isinstance(email, str) or not validate_email(email):
		raise ValueError("Enter a valid administrator email address.")
	return email


def _restore_empty_root(path, metadata):
	if metadata is None:
		return
	os.mkdir(path, stat.S_IMODE(metadata.st_mode))
	os.chown(path, metadata.st_uid, metadata.st_gid)


def _eligible_domains(env):
	from web_update import get_web_domains_info

	return {item["domain"]: item for item in get_web_domains_info(env) if item["static_enabled"]}


def _has_managed_nginx_include(path):
	try:
		with open(path, encoding="utf-8") as f:
			return f.readline() == MANAGED_NGINX_HEADER
	except OSError:
		return False


def get_wordpress_status(env):
	domains = _eligible_domains(env)
	status = []
	for domain in sorted(domains):
		root = wordpress_root(domain, env)
		include = wordpress_nginx_include(domain, env)
		config_exists = os.path.isfile(os.path.join(root, "wp-config.php"))
		civicrm_plugin = os.path.join(root, "wp-content", "plugins", "civicrm", "civicrm.php")
		include_exists = os.path.lexists(include)
		empty_root = not os.path.lexists(root) or (os.path.isdir(root) and not os.path.islink(root) and _is_empty_directory(root))
		status.append({
			"domain": domain,
			"installed": config_exists,
			"managed": config_exists and _has_managed_nginx_include(include),
			"can_install": wordpress_enabled(env) and not config_exists and not include_exists and empty_root,
			"civicrm_installed": os.path.isfile(civicrm_plugin),
			"can_install_civicrm": config_exists and env.get("INSTALL_CIVICRM") == "1" and not os.path.exists(civicrm_plugin),
		})
	return {"enabled": wordpress_enabled(env), "domains": status}


def install_wordpress(domain, title, admin_email, env):
	"""Create one new WordPress site and return its one-time admin password."""
	if not wordpress_enabled(env):
		raise ValueError("WordPress support is not enabled on this box.")
	title = _validate_title(title)
	admin_email = _validate_admin_email(admin_email)
	if domain not in _eligible_domains(env):
		raise ValueError("This domain is not eligible for static web hosting.")

	root = wordpress_root(domain, env)
	include = wordpress_nginx_include(domain, env)
	config = os.path.join(root, "wp-config.php")
	if os.path.isfile(config):
		raise ValueError("This domain already has a WordPress configuration.")
	if os.path.lexists(include):
		raise ValueError("This domain already has a custom nginx configuration.")
	if os.path.lexists(root):
		if os.path.islink(root) or not os.path.isdir(root):
			raise ValueError("This domain's web root is not a directory.")
		if not _is_empty_directory(root):
			raise ValueError("This domain's web root is not empty.")

	parent = os.path.dirname(root)
	os.makedirs(parent, mode=0o755, exist_ok=True)
	staging = os.path.join(parent, ".wordpress-install-" + secrets.token_hex(16))
	os.mkdir(staging, 0o750)
	try:
		_set_www_data_ownership(staging)
	except OSError:
		os.rmdir(staging)
		raise

	identifiers = wordpress_database_identifiers(domain)
	database_password = secrets.token_urlsafe(32)
	admin_password = secrets.token_urlsafe(24)
	include_created = False
	root_existed = os.path.lexists(root)
	root_metadata = os.stat(root, follow_symlinks=False) if root_existed else None
	root_moved = False
	database_creation_attempted = False
	installed = False
	try:
		_assert_database_objects_absent(identifiers)
		database_creation_attempted = True
		_create_database(identifiers, database_password)
		_run_wp(["core", "download", f"--path={staging}", "--locale=en_US"])
		_run_wp(["core", "verify-checksums", f"--path={staging}"])

		config_path = os.path.join(staging, "wp-config.php")
		with open(config_path, "w", encoding="utf-8") as f:
			f.write(wordpress_config(identifiers["database"], identifiers["user"], database_password))
		os.chmod(config_path, 0o600)
		_set_www_data_ownership(config_path)

		# WP-CLI reads prompted values from stdin, so the administrator password
		# is never present in a process list, shell command, or log message.
		_run_wp([
			"core", "install", f"--path={staging}", f"--url=https://{domain}", f"--title={title}",
			"--admin_user=admin", f"--admin_email={admin_email}", "--skip-email", "--prompt=admin_password",
		], stdin=(admin_password + "\n").encode("utf-8"))

		_write_exclusive(include, wordpress_nginx_config(), 0o644)
		include_created = True
		if root_existed:
			os.rmdir(root)
		os.rename(staging, root)
		root_moved = True

		from web_update import do_web_update
		do_web_update(env)
		installed = True
	except (OSError, subprocess.SubprocessError) as exc:
		raise ValueError("WordPress installation failed. No site was created.") from exc
	finally:
		if not installed:
			if root_moved:
				shutil.rmtree(root, ignore_errors=True)
				_restore_empty_root(root, root_metadata)
			if include_created:
				with contextlib.suppress(FileNotFoundError):
					os.unlink(include)
			shutil.rmtree(staging, ignore_errors=True)
			if database_creation_attempted:
				_remove_database(identifiers)

	return {
		"domain": domain,
		"url": "https://" + domain,
		"admin_user": "admin",
		"admin_password": admin_password,
	}
