#!/usr/bin/env bash
# Prepare a CloudShell/Linux environment for AWS PRM Resource Tagging.
# Outside an existing checkout, pass the repository URL as the first argument
# or set PRM_TAGGER_REPO_URL.
set -euo pipefail

repo_dir="${PRM_TAGGER_REPO_DIR:-aws-partner-tagging}"
repo_url="${1:-${PRM_TAGGER_REPO_URL:-}}"

echo "=== AWS PRM Resource Tagging bootstrap ==="

if [[ -f pyproject.toml && -d src/bs4it_tagging ]]; then
    project_dir="$(pwd)"
elif [[ -f "$repo_dir/pyproject.toml" && -d "$repo_dir/src/bs4it_tagging" ]]; then
    project_dir="$(cd "$repo_dir" && pwd)"
elif [[ -n "$repo_url" ]]; then
    if [[ -e "$repo_dir" ]]; then
        echo "ERROR: $repo_dir exists but is not a valid project checkout." >&2
        exit 1
    fi
    git clone -- "$repo_url" "$repo_dir"
    project_dir="$(cd "$repo_dir" && pwd)"
else
    echo "ERROR: project not found. Pass its Git URL:" >&2
    echo "  bash bootstrap.sh https://github.com/<owner>/aws-partner-tagging.git" >&2
    exit 1
fi

cd "$project_dir"

if command -v python3 >/dev/null 2>&1; then
    python_cmd="python3"
else
    python_cmd="python"
fi

"$python_cmd" -m pip install -e .

if [[ ! -f config/config.yaml ]]; then
    cp config/config.example.yaml config/config.yaml
    echo "Created config/config.yaml. ACTION REQUIRED: configure product code and scope before audit/apply."
else
    echo "Existing config/config.yaml preserved."
fi

"$python_cmd" -m bs4it_tagging --help

echo "Bootstrap complete. Edit config/config.yaml, then run: prm-tagger audit"
