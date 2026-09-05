"""Install CiviCRM into a managed WordPress site."""

import os
import subprocess

from wordpress import _wp_cli_path, wordpress_enabled, wordpress_root


def civicrm_enabled(env):
	return env.get("INSTALL_CIVICRM") == "1"


def civicrm_plugin_path(domain, env):
	return os.path.join(wordpress_root(domain, env), "wp-content", "plugins", "civicrm", "civicrm.php")


def install_civicrm(domain, env):
	if not wordpress_enabled(env) or not civicrm_enabled(env):
		raise ValueError("CiviCRM support is not enabled on this box.")
	root = wordpress_root(domain, env)
	from web_update import get_web_domains_info
	if domain not in {item["domain"] for item in get_web_domains_info(env) if item["static_enabled"]}:
		raise ValueError("This domain is not eligible for static web hosting.")
	if not os.path.isfile(os.path.join(root, "wp-config.php")):
		raise ValueError("Install WordPress for this domain before installing CiviCRM.")
	if os.path.exists(civicrm_plugin_path(domain, env)):
		raise ValueError("CiviCRM is already installed for this domain.")

	result = subprocess.run(
		["/usr/sbin/runuser", "-u", "www-data", "--", _wp_cli_path(), "plugin", "install", "civicrm", "--activate", "--path=" + root],
		stdout=subprocess.PIPE,
		stderr=subprocess.PIPE,
		text=True,
		check=False,
		timeout=300,
	)
	if result.returncode != 0 or not os.path.isfile(civicrm_plugin_path(domain, env)):
		raise ValueError("CiviCRM plugin installation failed.") from None
	return {
		"domain": domain,
		"installer_url": f"https://{domain}/wp-admin/options-general.php?page=civicrm-install",
	}
