#!/bin/bash
# Starts Neurology REDCap Extractor in its own window on a Mac (double-click in Finder). This Terminal window shows
# the app's messages; closing the app window closes the app. For no Terminal window at all, make the Mac app once:
#   .venv/bin/python scripts/make_mac_app.py
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then echo "Set up first: python3.11 -m venv .venv && .venv/bin/pip install -e \".[ner,app,window]\""; read -r; exit 1; fi
exec ./.venv/bin/python -m nmosd app --window
