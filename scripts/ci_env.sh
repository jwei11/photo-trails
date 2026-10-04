#!/bin/sh
# Print a .env for CI: .env.example with each change-me replaced by a random value, and
# the test database set. Usage: scripts/ci_env.sh > .env
set -eu
cd "$(dirname "$0")/.."
while IFS= read -r line; do
  case "$line" in
    *change-me*) echo "$line" | sed -E "s/change-me(-too)?/$(openssl rand -hex 16)/" ;;
    *) echo "$line" ;;
  esac
done < .env.example
