#!/usr/bin/env bash
set -euo pipefail
sudo chown -R vscode:vscode /home/vscode/.claude
pipx install hatch
hatch env create
curl -fsSL https://claude.ai/install.sh | bash   # Claude Code native installer
