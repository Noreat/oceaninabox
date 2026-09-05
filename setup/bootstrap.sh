#!/bin/bash
#########################################################
# This script is intended to be run like this:
#
#   curl https://Ocean3inaBox.email/setup.sh | sudo bash
#
#########################################################

if [ -z "$TAG" ]; then
	# If a version to install isn't explicitly given as an environment
	# variable, then install the latest version. But the latest version
	# depends on the machine's version of Ubuntu. Existing users need to
	# be able to upgrade to the latest version available for that version
	# of Ubuntu to satisfy the migration requirements.
	#
	# Also, the system status checks read this script for TAG = (without the
	# space, but if we put it in a comment it would confuse the status checks!)
	# to get the latest version, so the first such line must be the one that we
	# want to display in status checks.
	#
	# Allow point-release versions of the major releases, e.g. 24.04.1 is OK.
	UBUNTU_VERSION=$( lsb_release -d | sed 's/.*:\s*//' | sed 's/\([0-9]*\.[0-9]*\)\.[0-9]/\1/' )
	if [ "$UBUNTU_VERSION" == "Ubuntu 24.04 LTS" ]; then
		# Ubuntu 24.04 support is available on the current development branch.
		TAG=main
	elif [ "$UBUNTU_VERSION" == "Ubuntu 22.04 LTS" ]; then
		# This machine is running Ubuntu 22.04, which is supported by
		# Ocean3inaBox versions 60 and later.
		TAG=v76
	elif [ "$UBUNTU_VERSION" == "Ubuntu 18.04 LTS" ]; then
		# This machine is running Ubuntu 18.04, which is supported by
		# Ocean3inaBox versions 0.40 through 5x.
		echo "Support is ending for Ubuntu 18.04."
		echo "Please immediately begin to migrate your data to"
		echo "a new machine running Ubuntu 22.04. See:"
		echo "https://Ocean3inaBox.email/maintenance.html#upgrade"
		TAG=v57a
	elif [ "$UBUNTU_VERSION" == "Ubuntu 14.04 LTS" ]; then
		# This machine is running Ubuntu 14.04, which is supported by
		# Ocean3inaBox versions 1 through v0.30.
		echo "Ubuntu 14.04 is no longer supported."
		echo "The last version of Ocean3inaBox supporting Ubuntu 14.04 will be installed."
		TAG=v0.30
	else
		echo "This script may be used only on a machine running Ubuntu 14.04, 18.04, 22.04, or 24.04."
		exit 1
	fi
fi

# Are we running as root?
if [[ $EUID -ne 0 ]]; then
	echo "This script must be run as root. Did you leave out sudo?"
	exit 1
fi

# Clone the Ocean3inaBox repository if it doesn't exist.
if [ ! -d "$HOME/Ocean3inaBox" ]; then
	if [ ! -f /usr/bin/git ]; then
		echo "Installing git . . ."
		apt-get -q -q update
		DEBIAN_FRONTEND=noninteractive apt-get -q -q install -y git < /dev/null
		echo
	fi

	if [ "$SOURCE" == "" ]; then
		SOURCE=https://github.com/Noreat/oceaninabox.git
	fi

	echo "Downloading Ocean3inaBox $TAG. . ."
	git clone \
		-b "$TAG" --depth 1 \
		"$SOURCE" \
		"$HOME/Ocean3inaBox" \
		< /dev/null 2> /dev/null

	echo
fi

# Change directory to it.
cd "$HOME/Ocean3inaBox" || exit

# Update it.
if [ "$TAG" = "main" ] || [ "$TAG" != "$(git describe --always)" ]; then
	echo "Updating Ocean3inaBox to $TAG . . ."
	if [ "$TAG" = "main" ]; then
		git fetch --depth 1 --force --prune origin "$TAG"
	else
		git fetch --depth 1 --force --prune origin tag "$TAG"
	fi
	if [ "$TAG" = "main" ]; then
		git checkout -q -B "$TAG" FETCH_HEAD
	elif ! git checkout -q "$TAG"; then
		echo "Update failed. Did you modify something in $PWD?"
		exit 1
	fi
	echo
fi

# Start setup script.
setup/start.sh
