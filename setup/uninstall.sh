#!/bin/bash
# Remove all files, services, and packages installed by Ocean3inaBox.

set -euo pipefail

function require_safe_storage_root {
	case "$STORAGE_ROOT" in
		""|/|/etc|/home|/opt|/root|/tmp|/usr|/var)
			echo "ERROR: Refusing to delete unsafe STORAGE_ROOT: ${STORAGE_ROOT:-<empty>}."
			exit 1
			;;
	esac
}

function remove_ppa_if_present {
	local ppa=$1
	if grep -Rqs "$ppa" /etc/apt/sources.list /etc/apt/sources.list.d 2>/dev/null; then
		add-apt-repository --remove --yes "$ppa"
	fi
}

function uninstall_ocean3inabox {
	if [ "$(id -u)" != 0 ]; then
		echo "ERROR: Clean and new must be run as root."
		exit 1
	fi

	if [ -f /etc/Ocean3inaBox.conf ]; then
		source /etc/Ocean3inaBox.conf
	else
		# An interrupted cleanup has already removed the configuration. The
		# standard storage location is safe to retry and lets cleanup finish.
		echo "Ocean3inaBox configuration is absent; resuming an interrupted cleanup."
		STORAGE_USER=user-data
		STORAGE_ROOT=/home/user-data
	fi

	require_safe_storage_root

	echo "Stopping Ocean3inaBox services..."
	for service in Ocean3inaBox nginx php"${PHP_VER:-8.0}"-fpm postfix dovecot nsd bind9 opendkim opendmarc spampd spamassassin postgrey fail2ban munin-node mariadb; do
		if systemctl cat "$service" >/dev/null 2>&1; then
			systemctl disable --now "$service"
		fi
	done

	rm -f /etc/cron.d/Ocean3inaBox-nightly /etc/systemd/system/Ocean3inaBox.service /lib/systemd/system/Ocean3inaBox.service
	systemctl daemon-reload

	if command -v ufw >/dev/null; then
		ufw --force reset
		ufw disable
	fi

	echo "Removing Ocean3inaBox data and generated software..."
	rm -rf -- "$STORAGE_ROOT"
	rm -rf -- /usr/local/lib/Ocean3inaBox /usr/local/lib/owncloud /usr/local/lib/roundcubemail /usr/local/lib/z-push
	rm -rf -- /var/lib/Ocean3inaBox /var/log/Ocean3inaBox /var/tmp/roundcubemail
	rm -f /usr/local/bin/Ocean3inaBox /usr/local/bin/wp /usr/local/bin/wp-cli /usr/sbin/z-push-admin /usr/sbin/z-push-top
	rm -f /root/.ssh/id_rsa_miab /root/.ssh/id_rsa_miab.pub /etc/mailinabox-telegram.conf
	rm -f /etc/Ocean3inaBox.conf /etc/apt/apt.conf.d/02periodic

	# Restore the system resolver after removing the local Bind resolver.
	rm -f /etc/resolv.conf
	ln -s /run/systemd/resolve/stub-resolv.conf /etc/resolv.conf
	sed -i '/^[[:space:]]*DNSStubListener=no[[:space:]]*$/d' /etc/systemd/resolved.conf
	sed -i '/^[[:space:]]*fs\.inotify\.max_user_instances=1024[[:space:]]*$/d' /etc/sysctl.conf
	sed -i '\|^/swapfile[[:space:]]\+none[[:space:]]\+swap[[:space:]]\+sw|d' /etc/fstab
	if [ -f /swapfile ] && grep -q '^/swapfile' /proc/swaps; then
		swapoff /swapfile
	fi
	rm -f /swapfile

	echo "Removing Ocean3inaBox service packages..."
	DEBIAN_FRONTEND=noninteractive apt-get -y purge \
		bind9 certbot dbconfig-common dovecot-core dovecot-imapd dovecot-lmtpd dovecot-managesieved dovecot-pop3d dovecot-sieve \
		duplicity libawl-php \
		fail2ban mariadb-server munin munin-node nginx nsd opendkim opendkim-tools opendmarc \
		postfix postfix-pcre postfix-sqlite postgrey pyzor razor spamassassin spampd ufw \
		php8.0-cli php8.0-common php8.0-curl php8.0-fpm php8.0-gd php8.0-imap php8.0-intl php8.0-mbstring php8.0-mysql \
		php8.0-pspell php8.0-soap php8.0-sqlite3 php8.0-xml php8.0-zip
	DEBIAN_FRONTEND=noninteractive apt-get -y autoremove --purge

	if command -v pip3 >/dev/null; then
		for package in b2sdk boto3; do
			if pip3 show "$package" >/dev/null 2>&1; then
				if . /etc/os-release && [ "${VERSION_ID:-}" = "24.04" ]; then
					pip3 uninstall --break-system-packages --yes "$package"
				else
					pip3 uninstall --yes "$package"
				fi
			fi
		done
	fi

	remove_ppa_if_present ppa:duplicity-team/duplicity-release-git
	remove_ppa_if_present ppa:ondrej/php
	apt-get update
	systemctl enable --now systemd-resolved

	if id -u "$STORAGE_USER" >/dev/null 2>&1; then
		userdel --remove "$STORAGE_USER"
	fi
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
	uninstall_ocean3inabox
fi
