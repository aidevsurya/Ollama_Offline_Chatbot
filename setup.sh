#!/bin/bash
#set -e

com="pip install -r requirements.txt"

sudo apt update -y

read -p "Do you want full system Upgrade? (Y\n):" want_system_upgrade

if [[ "$want_system_upgrade" = "Y" || "$want_system_upgrade" = "y" ]]; then
    echo "Upgrading full system..."
	sudo apt upgrade -y
else
	echo "Skipping System Upgrade..."
fi

echo "Checking Requirements..."
$com
if [ $? = 0 ]; then
	echo "Python Requirements Successfully Checked..."
else
	echo "There is an error in checking Python Requirements..."
	read -p "Do you want to try with --break-system-packages? (Y\n):" want_break_system
	
	if [[ "$want_break_system" = "Y" || "$want_break_system" = "y" ]]; then
	    echo "Try Again..."
		pip install -r requirements.txt --break-system-packages
	else
		echo "Fix Manually or You should manually try - pip install -r requirements.txt..."
	fi
	
fi

echo "Requirements Checked..."

echo "Installing light weight model... You can install more manually"


if [command -v ollama &> /dev/null ]; then
	echo "Ollama Found... YES"
else
	echo "Ollama Not Found... Installing..."
	curl -fsSL https://ollama.com/install.sh | sh
fi

echo "---- Setup Completed ----"
