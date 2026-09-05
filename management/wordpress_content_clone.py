#!/usr/local/lib/Ocean3inaBox/env/bin/python3
"""Clone sanitized published WordPress posts and pages from a host over SSH."""

import argparse
import html
import json
import os
import pwd
import re
import secrets
import stat
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path


SSH_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]*[$]?$", re.IGNORECASE)
HOST_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*$", re.IGNORECASE)
SOURCE_PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]+$")
MAX_SOURCE_JSON_BYTES = 32 * 1024 * 1024
MAX_POSTS = 10000
MAX_TITLE_LENGTH = 255
MAX_CONTENT_LENGTH = 1024 * 1024
RUNUSER = "/usr/sbin/runuser"
PHP = "/usr/bin/php"


class _TextOnlyHTMLParser(HTMLParser):
	"""Extract displayed text while dropping elements that are not article content."""

	BLOCK_TAGS = {
		"address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
		"figcaption", "figure", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "li",
		"main", "ol", "p", "pre", "section", "table", "td", "th", "tr", "ul",
	}
	NON_CONTENT_CONTAINERS = {
		"canvas", "embed", "form", "head", "iframe", "math", "object", "script",
		"select", "style", "svg", "template", "textarea", "video", "audio",
	}

	def __init__(self):
		super().__init__(convert_charrefs=True)
		self.parts = []
		self.non_content_depth = 0

	def handle_starttag(self, tag, attrs):
		tag = tag.lower()
		if tag in self.NON_CONTENT_CONTAINERS:
			self.non_content_depth += 1
		elif not self.non_content_depth and tag in self.BLOCK_TAGS:
			self.parts.append("\n")

	def handle_startendtag(self, tag, attrs):
		if not self.non_content_depth and tag.lower() == "br":
			self.parts.append("\n")

	def handle_endtag(self, tag):
		tag = tag.lower()
		if tag in self.NON_CONTENT_CONTAINERS and self.non_content_depth:
			self.non_content_depth -= 1
		elif not self.non_content_depth and tag in self.BLOCK_TAGS:
			self.parts.append("\n")

	def handle_data(self, data):
		if not self.non_content_depth:
			self.parts.append(data)


def validate_ssh_target(host: str, user: str) -> None:
	if not isinstance(host, str) or not HOST_RE.fullmatch(host) or ".." in host or host.startswith(".") or host.endswith("."):
		raise ValueError("The source host must be a hostname or IPv4 address.")
	if not isinstance(user, str) or not SSH_USER_RE.fullmatch(user):
		raise ValueError("The SSH user contains unsupported characters.")


def validate_source_wordpress_path(path: str) -> str:
	if (
		not isinstance(path, str)
		or len(path) > 1024
		or not SOURCE_PATH_RE.fullmatch(path)
		or any(part in {".", ".."} for part in path.split("/"))
	):
		raise ValueError("The source WordPress path must be an absolute path without traversal components.")
	return path


def validate_destination_domain(domain: str) -> str:
	if (
		not isinstance(domain, str)
		or len(domain) > 253
		or domain.lower() != domain
		or not HOST_RE.fullmatch(domain)
		or ".." in domain
		or domain.startswith(".")
		or domain.endswith(".")
		or any(not label or label.startswith("-") or label.endswith("-") for label in domain.split("."))
	):
		raise ValueError("The destination domain must be a lowercase hostname.")
	return domain


def sanitize_html(value: str) -> str:
	"""Return source text as minimal, attribute-free HTML safe for WordPress content."""
	if not isinstance(value, str):
		raise ValueError("WordPress text fields must be strings.")

	parser = _TextOnlyHTMLParser()
	parser.feed(value)
	parser.close()
	text = "".join(parser.parts).replace("\r\n", "\n").replace("\r", "\n")
	lines = []
	for line in text.splitlines():
		line = "".join(char for char in line if char.isprintable() or char in "\t ")
		line = re.sub(r"\s+", " ", line).strip()
		if line:
			lines.append(line)
	return "".join("<p>" + html.escape(line, quote=False) + "</p>" for line in lines)


def sanitize_title(value: str) -> str:
	return re.sub(r"</?p>", " ", sanitize_html(value)).strip()


def source_wp_command(source_wordpress_path: str) -> list[str]:
	"""Build the sole permitted remote command after validating its only variable."""
	source_wordpress_path = validate_source_wordpress_path(source_wordpress_path)
	return [
		"wp", "--allow-root", "--skip-plugins", "--skip-themes", f"--path={source_wordpress_path}",
		"post", "list", "--post_type=post,page", "--post_status=publish",
		"--posts_per_page=-1", "--fields=post_type,post_title,post_content", "--format=json",
	]


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
		timeout=120,
	)
	if result.returncode != 0:
		raise RuntimeError(f"Source WordPress command failed: {result.stderr.strip() or 'SSH connection failed.'}")
	if len(result.stdout.encode("utf-8")) > MAX_SOURCE_JSON_BYTES:
		raise ValueError("Source content exceeds the 32 MiB import safety limit.")
	return result.stdout


def get_source_posts(host: str, user: str, identity_file: Path | None, source_wordpress_path: str) -> list[dict[str, str]]:
	try:
		records = json.loads(run_ssh(host, user, identity_file, source_wp_command(source_wordpress_path)))
	except json.JSONDecodeError as exc:
		raise ValueError("Source WordPress did not return a JSON post list.") from exc
	if not isinstance(records, list):
		raise ValueError("Source WordPress did not return a JSON list.")
	if len(records) > MAX_POSTS:
		raise ValueError(f"Source has more than the {MAX_POSTS} post import safety limit.")

	posts = []
	for record in records:
		if not isinstance(record, dict) or record.get("post_type") not in {"post", "page"}:
			raise ValueError("Source WordPress returned an unsupported post record.")
		title = sanitize_title(record.get("post_title"))
		content = sanitize_html(record.get("post_content"))
		if len(title) > MAX_TITLE_LENGTH:
			raise ValueError("A source post title exceeds the WordPress title length limit.")
		if len(content) > MAX_CONTENT_LENGTH:
			raise ValueError("A source post exceeds the 1 MiB content safety limit.")
		posts.append({"type": record["post_type"], "title": title, "content": content})
	return posts


IMPORTER = r"""<?php
try {
	$posts = json_decode(stream_get_contents(STDIN), true, 512, JSON_THROW_ON_ERROR);
	if (!is_array($posts)) {
		throw new RuntimeException('Invalid import payload.');
	}
	require __DIR__ . '/wp-load.php';
	$existing = get_posts(array(
		'post_type' => array('post', 'page'),
		'post_status' => array('publish', 'future', 'draft', 'pending', 'private', 'trash', 'auto-draft', 'inherit'),
		'posts_per_page' => 1,
		'fields' => 'ids',
		'suppress_filters' => true,
	));
	if ($existing) {
		throw new RuntimeException('The destination already contains a post or page.');
	}
	if ($wpdb->query('START TRANSACTION') === false) {
		throw new RuntimeException('Could not start the WordPress import transaction.');
	}
	foreach ($posts as $post) {
		if (!is_array($post) || !in_array($post['type'] ?? null, array('post', 'page'), true)
			|| !is_string($post['title'] ?? null) || !is_string($post['content'] ?? null)) {
			throw new RuntimeException('Invalid sanitized post payload.');
		}
		$id = wp_insert_post(array(
			'post_type' => $post['type'],
			'post_status' => 'publish',
			'post_title' => $post['title'],
			'post_content' => $post['content'],
		), true);
		if (is_wp_error($id)) {
			throw new RuntimeException($id->get_error_message());
		}
	}
	if ($wpdb->query('COMMIT') === false) {
		throw new RuntimeException('Could not commit the WordPress import transaction.');
	}
	echo count($posts) . "\n";
} catch (Throwable $error) {
	if (isset($wpdb)) {
		$wpdb->query('ROLLBACK');
	}
	fwrite(STDERR, $error->getMessage() . "\n");
	exit(1);
}
"""


def get_target_root(destination_domain: str) -> str:
	import utils
	import wordpress

	env = utils.load_environment()
	root = wordpress.wordpress_root(destination_domain, env)
	required_files = ("wp-config.php", "wp-load.php")
	if os.path.islink(root) or not os.path.isdir(root):
		raise ValueError("The destination WordPress root is not a directory.")
	if any(os.path.islink(os.path.join(root, filename)) or not os.path.isfile(os.path.join(root, filename)) for filename in required_files):
		raise ValueError("The destination does not contain a configured WordPress installation.")
	return root


def _write_importer(root: str) -> str:
	filename = os.path.join(root, ".o3ib-wordpress-import-" + secrets.token_hex(32) + ".php")
	fd = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
	try:
		with os.fdopen(fd, "w", encoding="utf-8") as f:
			f.write(IMPORTER)
			f.flush()
			os.fsync(f.fileno())
		account = pwd.getpwnam("www-data")
		os.chown(filename, account.pw_uid, account.pw_gid)
	except OSError:
		try:
			os.unlink(filename)
		except FileNotFoundError:
			pass
		raise
	return filename


def import_posts(root: str, posts: list[dict[str, str]]) -> None:
	importer = _write_importer(root)
	try:
		result = subprocess.run(
			[RUNUSER, "-u", "www-data", "--", PHP, importer],
			input=json.dumps(posts, ensure_ascii=False).encode("utf-8"),
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
			timeout=120,
			check=False,
		)
		if result.returncode != 0:
			message = result.stderr.decode("utf-8", "replace").strip()
			raise RuntimeError(f"WordPress import failed: {message or 'PHP importer failed.'}")
	finally:
		try:
			os.unlink(importer)
		except FileNotFoundError:
			pass


def validate_identity_file(identity_file: Path | None) -> None:
	if identity_file is None:
		return
	if not identity_file.is_file():
		raise ValueError(f"{identity_file} is not a readable SSH private key file.")
	if stat.S_IMODE(identity_file.stat().st_mode) & 0o077:
		raise ValueError(f"{identity_file} must not be accessible to group or other users (use chmod 600).")


def main() -> int:
	parser = argparse.ArgumentParser(
		description="Clone sanitized published WordPress posts and pages from a source host over SSH.",
		epilog="Run with --dry-run first. A normal import refuses a destination with any posts or pages.",
	)
	parser.add_argument("--source-host", required=True, help="Source hostname or IPv4 address used for SSH.")
	parser.add_argument("--ssh-user", default="root", help="SSH user permitted to run the fixed WP-CLI command.")
	parser.add_argument("--ssh-identity", type=Path, help="Mode-0600 private SSH key for the source host.")
	parser.add_argument("--source-wordpress-path", required=True, help="Absolute source WordPress path without traversal.")
	parser.add_argument("--destination-domain", required=True, help="Configured lowercase WordPress domain on this box.")
	parser.add_argument("--dry-run", action="store_true", help="Validate and count sanitized source content without writing.")
	args = parser.parse_args()

	try:
		if os.geteuid() != 0:
			raise ValueError("This command must be run as root.")
		validate_ssh_target(args.source_host, args.ssh_user)
		validate_source_wordpress_path(args.source_wordpress_path)
		validate_destination_domain(args.destination_domain)
		validate_identity_file(args.ssh_identity)
		get_target_root(args.destination_domain)
		posts = get_source_posts(args.source_host, args.ssh_user, args.ssh_identity, args.source_wordpress_path)
		print(f"Discovered {len(posts)} sanitized published posts/pages.")
		if args.dry_run:
			return 0
		if posts:
			import_posts(get_target_root(args.destination_domain), posts)
		print(f"Imported {len(posts)} sanitized published posts/pages into {args.destination_domain}.")
		return 0
	except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
		print(f"WordPress content clone failed: {exc}", file=sys.stderr)
		return 1


if __name__ == "__main__":
	sys.exit(main())
