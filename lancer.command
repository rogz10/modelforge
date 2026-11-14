#!/bin/zsh
# Double-clique sur ce fichier pour lancer ModelForge.
cd "$(dirname "$0")"
exec .venv/bin/python -m modelforge "$@"
