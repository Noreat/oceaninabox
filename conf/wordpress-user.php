<?php
if ($argc < 3) {
	fwrite(STDERR, "Usage: wordpress-user.php <create|set-password|remove> <email>\n");
	exit(1);
}

require_once '/home/user-data/www/default/wp-load.php';
require_once ABSPATH . 'wp-admin/includes/user.php';

global $wpdb;
if ($wpdb->get_var("SHOW TABLES LIKE '{$wpdb->users}'") !== $wpdb->users) {
	fwrite(STDERR, "WordPress has not been configured yet.\n");
	exit(1);
}

$action = $argv[1];
$email = $argv[2];
$user = get_user_by('email', $email);

if ($action === 'create') {
	if ($user) {
		exit(0);
	}
	$password = trim(stream_get_contents(STDIN));
	$username = substr($email, 0, 50) . '-' . substr(sha1($email), 0, 9);
	$user_id = wp_create_user($username, $password, $email);
	if (is_wp_error($user_id)) {
		fwrite(STDERR, $user_id->get_error_message() . "\n");
		exit(1);
	}
	wp_update_user(array('ID' => $user_id, 'role' => 'subscriber'));
	exit(0);
}

if ($action === 'set-password') {
	if ($user) {
		wp_set_password(trim(stream_get_contents(STDIN)), $user->ID);
	}
	exit(0);
}

if ($action === 'remove') {
	if ($user && !wp_delete_user($user->ID)) {
		fwrite(STDERR, "Could not remove the WordPress user.\n");
		exit(1);
	}
	exit(0);
}

fwrite(STDERR, "Unknown action.\n");
exit(1);
