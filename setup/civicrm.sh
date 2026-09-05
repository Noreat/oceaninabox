#!/bin/bash
# Dependencies required by the CiviCRM WordPress plugin.

source setup/functions.sh
source /etc/Ocean3inaBox.conf

if [ "$INSTALL_WORDPRESS" != 1 ]; then
	echo "CiviCRM support requires WordPress support."
	exit 1
fi

echo "Installing CiviCRM support..."
apt_install php"${PHP_VER}"-curl php"${PHP_VER}"-gd php"${PHP_VER}"-intl php"${PHP_VER}"-mbstring php"${PHP_VER}"-xml php"${PHP_VER}"-zip
restart_service php"$PHP_VER"-fpm
