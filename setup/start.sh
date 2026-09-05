#!/bin/bash
# This is the entry point for configuring the system.
#####################################################

source setup/functions.sh # load our functions

# Check system setup: Are we running as root on Ubuntu 18.04 on a
# machine with enough memory? Is /tmp mounted with exec.
# If not, this shows an error and exits.
source setup/preflight.sh

# Ensure Python reads/writes files in UTF-8. If the machine
# triggers some other locale in Python, like ASCII encoding,
# Python may not be able to read/write files. This is also
# in the management daemon startup script and the cron script.

if ! locale -a | grep en_US.utf8 > /dev/null; then
    # Generate locale if not exists
    hide_output locale-gen en_US.UTF-8
fi

export LANGUAGE=en_US.UTF-8
export LC_ALL=en_US.UTF-8
export LANG=en_US.UTF-8
export LC_TYPE=en_US.UTF-8

# Fix so line drawing characters are shown correctly in Putty on Windows. See #744.
export NCURSES_NO_UTF8_ACS=1

# Recall the last settings used if we're running this a second time.
if [ -f /etc/Ocean3inaBox.conf ]; then
	# Run any system migrations before proceeding. Since this is a second run,
	# we assume we have Python already installed.
	setup/migrate.py --migrate || exit 1

	# Load the old .conf file to get existing configuration options loaded
	# into variables with a DEFAULT_ prefix.
	cat /etc/Ocean3inaBox.conf | sed s/^/DEFAULT_/ > /tmp/Ocean3inaBox.prev.conf
	source /tmp/Ocean3inaBox.prev.conf
	rm -f /tmp/Ocean3inaBox.prev.conf
else
	FIRST_TIME_SETUP=1
fi

# Put a start script in a global location. We tell the user to run 'Ocean3inaBox'
# in the first dialog prompt, so we should do this before that starts.
cat > /usr/local/bin/Ocean3inaBox << EOF;
#!/bin/bash
cd $PWD
source setup/start.sh
EOF
chmod +x /usr/local/bin/Ocean3inaBox

# Ask the user for the PRIMARY_HOSTNAME, PUBLIC_IP, and PUBLIC_IPV6,
# if values have not already been set in environment variables. When running
# non-interactively, be sure to set values for all! Also sets STORAGE_USER and
# STORAGE_ROOT.
source setup/questions.sh

# Run some network checks to make sure setup on this machine makes sense.
# Skip on existing installs since we don't want this to block the ability to
# upgrade, and these checks are also in the control panel status checks.
if [ -z "${DEFAULT_PRIMARY_HOSTNAME:-}" ]; then
if [ -z "${SKIP_NETWORK_CHECKS:-}" ]; then
	source setup/network-checks.sh
fi
fi

# Create the STORAGE_USER and STORAGE_ROOT directory if they don't already exist.
#
# Set the directory and all of its parent directories' permissions to world
# readable since it holds files owned by different processes.
#
# If the STORAGE_ROOT is missing the Ocean3inaBox.version file that lists a
# migration (schema) number for the files stored there, assume this is a fresh
# installation to that directory and write the file to contain the current
# migration number for this version of Ocean3inaBox.
if ! id -u "$STORAGE_USER" >/dev/null 2>&1; then
	useradd -m "$STORAGE_USER"
fi
if [ ! -d "$STORAGE_ROOT" ]; then
	mkdir -p "$STORAGE_ROOT"
fi
f=$STORAGE_ROOT
while [[ $f != / ]]; do chmod a+rx "$f"; f=$(dirname "$f"); done;
if [ ! -f "$STORAGE_ROOT/Ocean3inaBox.version" ]; then
	setup/migrate.py --current > "$STORAGE_ROOT/Ocean3inaBox.version"
	chown "$STORAGE_USER:$STORAGE_USER" "$STORAGE_ROOT/Ocean3inaBox.version"
fi

# Save the global options in /etc/Ocean3inaBox.conf so that standalone
# tools know where to look for data. The default MTA_STS_MODE setting
# is blank unless set by an environment variable, but see web.sh for
# how that is interpreted.
cat > /etc/Ocean3inaBox.conf << EOF;
STORAGE_USER=$STORAGE_USER
STORAGE_ROOT=$STORAGE_ROOT
PRIMARY_HOSTNAME=$PRIMARY_HOSTNAME
WEBMAIL_HOSTNAME=$WEBMAIL_HOSTNAME
PUBLIC_IP=$PUBLIC_IP
PUBLIC_IPV6=$PUBLIC_IPV6
PRIVATE_IP=$PRIVATE_IP
PRIVATE_IPV6=$PRIVATE_IPV6
MTA_STS_MODE=${DEFAULT_MTA_STS_MODE:-enforce}
INSTALL_NEXTCLOUD=$INSTALL_NEXTCLOUD
INSTALL_WORDPRESS=$INSTALL_WORDPRESS
EOF

SETUP_STATE_FILE=/var/lib/Ocean3inaBox/setup-state

function set_next_setup_step {
	local step=$1
	local temporary_state_file="$SETUP_STATE_FILE.tmp"

	umask 077
	mkdir -p "$(dirname "$SETUP_STATE_FILE")"
	printf '%s\n' "$step" > "$temporary_state_file"
	mv "$temporary_state_file" "$SETUP_STATE_FILE"
}

function run_setup_step {
	local step=$1
	local description=$2
	local script=$3

	if [ "$NEXT_SETUP_STEP" -le "$step" ]; then
		echo
		echo "Running setup step: $description"
		source "$script"
		NEXT_SETUP_STEP=$((step + 1))
		set_next_setup_step "$NEXT_SETUP_STEP"
	fi
}

if [ -f "$SETUP_STATE_FILE" ]; then
	NEXT_SETUP_STEP=$(cat "$SETUP_STATE_FILE")
	if ! [[ "$NEXT_SETUP_STEP" =~ ^[1-9][0-9]*$ ]] || [ "$NEXT_SETUP_STEP" -gt 22 ]; then
		echo "ERROR: Invalid setup resume state in $SETUP_STATE_FILE."
		exit 1
	fi
	if [ -z "${NONINTERACTIVE:-}" ]; then
		input_menu "Resume Ocean3inaBox Setup" \
			"A previous setup run stopped at step $NEXT_SETUP_STEP. What do you want to do?" \
			"resume^Resume from the first unfinished step^restart^Run all setup steps again" \
			SETUP_ACTION
		if [ "$SETUP_ACTION_EXITCODE" -ne 0 ]; then
			exit
		fi
		if [ "$SETUP_ACTION" = restart ]; then
			NEXT_SETUP_STEP=1
			set_next_setup_step "$NEXT_SETUP_STEP"
		fi
	fi
	if [ "$NEXT_SETUP_STEP" = 1 ]; then
		echo "Running all Ocean3inaBox setup steps."
	else
		echo "Resuming Ocean3inaBox setup at step $NEXT_SETUP_STEP."
	fi
else
	NEXT_SETUP_STEP=1
	set_next_setup_step "$NEXT_SETUP_STEP"
fi

# Start service configuration. Each script is idempotent, so a failed step can
# be safely rerun without repeating the successfully completed earlier steps.
run_setup_step 1 "system configuration" setup/system.sh
run_setup_step 2 "TLS configuration" setup/ssl.sh
run_setup_step 3 "DNS configuration" setup/dns.sh
run_setup_step 4 "Postfix configuration" setup/mail-postfix.sh
run_setup_step 5 "Dovecot configuration" setup/mail-dovecot.sh
run_setup_step 6 "mail user configuration" setup/mail-users.sh
run_setup_step 7 "DKIM configuration" setup/dkim.sh
run_setup_step 8 "spam filtering configuration" setup/spamassassin.sh
run_setup_step 9 "web server configuration" setup/web.sh
run_setup_step 10 "webmail configuration" setup/webmail.sh
if [ "$NEXT_SETUP_STEP" -le 11 ]; then
	if [ "$INSTALL_NEXTCLOUD" = 1 ]; then
		run_setup_step 11 "Nextcloud configuration" setup/nextcloud.sh
	else
		echo "Skipping Nextcloud configuration."
		NEXT_SETUP_STEP=12
		set_next_setup_step "$NEXT_SETUP_STEP"
	fi
fi
run_setup_step 12 "Z-Push configuration" setup/zpush.sh
run_setup_step 13 "management service configuration" setup/management.sh
run_setup_step 14 "Munin configuration" setup/munin.sh

# Wait for the management daemon to start...
if [ "$NEXT_SETUP_STEP" -le 15 ]; then
	until nc -z -w 4 127.0.0.1 10222
	do
		echo "Waiting for the Ocean3inaBox management daemon to start..."
		sleep 2
	done
	NEXT_SETUP_STEP=16
	set_next_setup_step "$NEXT_SETUP_STEP"
fi

# ...and then have it write the DNS and nginx configuration files and start those
# services.
if [ "$NEXT_SETUP_STEP" -le 16 ]; then
	tools/dns_update
	NEXT_SETUP_STEP=17
	set_next_setup_step "$NEXT_SETUP_STEP"
fi
if [ "$NEXT_SETUP_STEP" -le 17 ]; then
	tools/web_update
	NEXT_SETUP_STEP=18
	set_next_setup_step "$NEXT_SETUP_STEP"
fi

# Give fail2ban another restart. The log files may not all have been present when
# fail2ban was first configured, but they should exist now.
if [ "$NEXT_SETUP_STEP" -le 18 ]; then
	restart_service fail2ban
	NEXT_SETUP_STEP=19
	set_next_setup_step "$NEXT_SETUP_STEP"
fi

# If there aren't any mail users yet, create one.
if [ "$NEXT_SETUP_STEP" -le 19 ]; then
	source setup/firstuser.sh
	NEXT_SETUP_STEP=20
	set_next_setup_step "$NEXT_SETUP_STEP"
fi

# Register with Let's Encrypt, including agreeing to the Terms of Service.
# We'd let certbot ask the user interactively, but when this script is
# run in the recommended curl-pipe-to-bash method there is no TTY and
# certbot will fail if it tries to ask.
if [ "$NEXT_SETUP_STEP" -le 20 ]; then
	if [ ! -d "$STORAGE_ROOT/ssl/lets_encrypt/accounts/acme-v02.api.letsencrypt.org/" ]; then
		echo
		echo "-----------------------------------------------"
		echo "Ocean3inaBox uses Let's Encrypt to provision free SSL/TLS certificates"
		echo "to enable HTTPS connections to your box. We're automatically"
		echo "agreeing you to their subscriber agreement. See https://letsencrypt.org."
		echo
		certbot register --register-unsafely-without-email --agree-tos --config-dir "$STORAGE_ROOT/ssl/lets_encrypt"
	fi
	management/ssl_certificates.py
	NEXT_SETUP_STEP=21
	set_next_setup_step "$NEXT_SETUP_STEP"
fi

if [ "$NEXT_SETUP_STEP" -le 21 ]; then
	for service in bind9 dovecot fail2ban mariadb munin nginx nsd Ocean3inaBox opendkim opendmarc php"$PHP_VER"-fpm postfix spampd; do
		restart_service "$service"
	done
	NEXT_SETUP_STEP=22
	set_next_setup_step "$NEXT_SETUP_STEP"
fi

# Done.
rm -f "$SETUP_STATE_FILE"
echo
echo "-----------------------------------------------"
echo
echo "Your Ocean3inaBox is running."
echo
echo "Please log in to the control panel for further instructions at:"
echo
if management/status_checks.py --check-primary-hostname; then
	# Show the nice URL if it appears to be resolving and has a valid certificate.
	echo "https://$PRIMARY_HOSTNAME/admin"
	echo
	echo "If you have a DNS problem put the box's IP address in the URL"
	echo "(https://$PUBLIC_IP/admin) but then check the TLS fingerprint:"
	openssl x509 -in "$STORAGE_ROOT/ssl/ssl_certificate.pem" -noout -fingerprint -sha256\
        	| sed "s/SHA256 Fingerprint=//i"
else
	echo "https://$PUBLIC_IP/admin"
	echo
	echo "You will be alerted that the website has an invalid certificate. Check that"
	echo "the certificate fingerprint matches:"
	echo
	openssl x509 -in "$STORAGE_ROOT/ssl/ssl_certificate.pem" -noout -fingerprint -sha256\
        	| sed "s/SHA256 Fingerprint=//i"
	echo
	echo "Then you can confirm the security exception and continue."
	echo
fi
