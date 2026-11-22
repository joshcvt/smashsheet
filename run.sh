#!/bin/sh
# Wrapper that appends output to smashsheet.log; usable directly or from cron.
cd "$(dirname "$0")" && exec .venv/bin/python smashsheet.py "$@" >> smashsheet.log 2>&1
