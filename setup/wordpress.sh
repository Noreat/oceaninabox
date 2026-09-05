#!/bin/bash
# WordPress website
# -----------------

source setup/functions.sh
source /etc/Ocean3inaBox.conf

WORDPRESS_VERSION=7.1
WORDPRESS_HASH=b2b81d9242a122a8c7104a92387794eb64fcde97
WORDPRESS_ROOT=$STORAGE_ROOT/www/default
WORDPRESS_CONFIG_DIR=$STORAGE_ROOT/wordpress
WORDPRESS_DATABASE=wordpress
WORDPRESS_DATABASE_USER=wordpress
WORDPRESS_DATABASE_PASSWORD_FILE=$WORDPRESS_CONFIG_DIR/database-password
WORDPRESS_USER_SCRIPT=/usr/local/lib/Ocean3inaBox/wordpress-user.php

echo "Installing WordPress..."
apt_install mariadb-server php"${PHP_VER}"-mysql php"${PHP_VER}"-curl php"${PHP_VER}"-gd \
	php"${PHP_VER}"-mbstring php"${PHP_VER}"-xml php"${PHP_VER}"-zip
systemctl enable mariadb
restart_service mariadb

mkdir -p "$WORDPRESS_ROOT"
if [ ! -d "$WORDPRESS_ROOT/wp-admin" ]; then
	if [ -f "$WORDPRESS_ROOT/index.html" ]; then
		if cmp -s "$WORDPRESS_ROOT/index.html" conf/www_default.html; then
			rm -f "$WORDPRESS_ROOT/index.html"
		else
			echo "ERROR: Refusing to replace the existing website at $WORDPRESS_ROOT."
			exit 1
		fi
	fi
	if [ -n "$(find "$WORDPRESS_ROOT" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
		echo "ERROR: Refusing to install WordPress into the non-empty directory $WORDPRESS_ROOT."
		exit 1
	fi

	WORDPRESS_INSTALL_DIRECTORY=$(mktemp -d "$STORAGE_ROOT/.wordpress-install.XXXXXX")
	wget_verify "https://wordpress.org/wordpress-$WORDPRESS_VERSION.zip" "$WORDPRESS_HASH" "$WORDPRESS_INSTALL_DIRECTORY/wordpress.zip"
	unzip -q "$WORDPRESS_INSTALL_DIRECTORY/wordpress.zip" -d "$WORDPRESS_INSTALL_DIRECTORY"
	rm -f "$WORDPRESS_INSTALL_DIRECTORY/wordpress.zip"
	rmdir "$WORDPRESS_ROOT"
	mv "$WORDPRESS_INSTALL_DIRECTORY/wordpress" "$WORDPRESS_ROOT"
	rmdir "$WORDPRESS_INSTALL_DIRECTORY"
fi

if [ ! -f "$WORDPRESS_DATABASE_PASSWORD_FILE" ]; then
	mkdir -p "$WORDPRESS_CONFIG_DIR"
	umask 077
	openssl rand -hex 32 > "$WORDPRESS_DATABASE_PASSWORD_FILE"
fi
WORDPRESS_DATABASE_PASSWORD=$(<"$WORDPRESS_DATABASE_PASSWORD_FILE")

mysql <<EOF
CREATE DATABASE IF NOT EXISTS $WORDPRESS_DATABASE CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS '$WORDPRESS_DATABASE_USER'@'localhost' IDENTIFIED BY '$WORDPRESS_DATABASE_PASSWORD';
ALTER USER '$WORDPRESS_DATABASE_USER'@'localhost' IDENTIFIED BY '$WORDPRESS_DATABASE_PASSWORD';
GRANT ALL PRIVILEGES ON $WORDPRESS_DATABASE.* TO '$WORDPRESS_DATABASE_USER'@'localhost';
FLUSH PRIVILEGES;
EOF

if [ ! -f "$WORDPRESS_ROOT/wp-config.php" ]; then
	WORDPRESS_SALTS=$(for key in AUTH_KEY SECURE_AUTH_KEY LOGGED_IN_KEY NONCE_KEY AUTH_SALT SECURE_AUTH_SALT LOGGED_IN_SALT NONCE_SALT; do
		printf "define('%s', '%s');\n" "$key" "$(openssl rand -base64 48 | tr -d '\n')"
	done)
	cat > "$WORDPRESS_ROOT/wp-config.php" <<EOF
<?php
define('DB_NAME', '$WORDPRESS_DATABASE');
define('DB_USER', '$WORDPRESS_DATABASE_USER');
define('DB_PASSWORD', '$WORDPRESS_DATABASE_PASSWORD');
define('DB_HOST', 'localhost');
define('DB_CHARSET', 'utf8mb4');
define('DB_COLLATE', '');
$WORDPRESS_SALTS
\$table_prefix = 'wp_';
define('WP_DEBUG', false);
if (!defined('ABSPATH')) {
	define('ABSPATH', __DIR__ . '/');
}
require_once ABSPATH . 'wp-settings.php';
EOF
	chown root:www-data "$WORDPRESS_ROOT/wp-config.php"
	chmod 640 "$WORDPRESS_ROOT/wp-config.php"

	mkdir -p "$(dirname "$WORDPRESS_USER_SCRIPT")"
	sed "s#/home/user-data/www/default#$WORDPRESS_ROOT#" conf/wordpress-user.php > "$WORDPRESS_USER_SCRIPT"
	chown root:root "$WORDPRESS_USER_SCRIPT"
	chmod 700 "$WORDPRESS_USER_SCRIPT"
fi

chown -R root:www-data "$WORDPRESS_ROOT"
find "$WORDPRESS_ROOT" -type d -exec chmod 755 {} +
find "$WORDPRESS_ROOT" -type f -exec chmod 644 {} +
chown -R www-data:www-data "$WORDPRESS_ROOT/wp-content"
chown root:www-data "$WORDPRESS_ROOT/wp-config.php"
chmod 640 "$WORDPRESS_ROOT/wp-config.php"
