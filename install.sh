#!/usr/bin/env bash
# Installs meetscribe for the current user (no sudo):
#   ~/.local/share/meetscribe/venv   the program
#   ~/.local/bin/meetscribe          launcher
#   ~/.config/systemd/user/          the "watch" service (installed, not enabled)
set -euo pipefail
cd "$(dirname "$0")"

missing=()
command -v pactl >/dev/null && command -v parec >/dev/null || missing+=(pulseaudio-utils)
command -v ffmpeg >/dev/null || missing+=(ffmpeg)
command -v notify-send >/dev/null || missing+=(libnotify-bin)
python3 -c 'import venv, ensurepip' 2>/dev/null || missing+=(python3-venv)
if ((${#missing[@]})); then
    echo "Missing system packages. Run this, then re-run install.sh:"
    echo "    sudo apt install ${missing[*]}"
    exit 1
fi

python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || {
    echo "Python 3.10 or newer is required (found $(python3 -V))."
    exit 1
}

venv="$HOME/.local/share/meetscribe/venv"
python3 -m venv "$venv"
"$venv/bin/pip" install --quiet --upgrade pip
"$venv/bin/pip" install --quiet .

mkdir -p "$HOME/.local/bin" "$HOME/.config/systemd/user" "$HOME/.config/meetscribe/skills"
ln -sf "$venv/bin/meetscribe" "$HOME/.local/bin/meetscribe"
install -m 644 contrib/meetscribe.service "$HOME/.config/systemd/user/meetscribe.service"
systemctl --user daemon-reload 2>/dev/null || true

env_file="$HOME/.config/meetscribe/env"
if [[ ! -e "$env_file" ]]; then
    (umask 177; printf '%s\n' '# Environment for meetscribe, one KEY=VALUE per line.' \
        '# Bedrock (default): the usual AWS credential chain is used; set what you need.' \
        '# AWS_REGION=us-west-2' '# AWS_PROFILE=' '# AWS_BEARER_TOKEN_BEDROCK=' \
        '# Claude API instead (provider = "anthropic"):' '# ANTHROPIC_API_KEY=' >"$env_file")
fi

cat <<EOF

Installed. Next:
  1. Make AWS credentials with Bedrock access available (aws configure / aws sso login),
     and set AWS_REGION in $env_file if your AWS profile has no region
  2. meetscribe doctor                 check the setup
  3. meetscribe record                 record a meeting now (Ctrl-C to stop)
     systemctl --user enable --now meetscribe     or record every browser call automatically
EOF
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "Note: add ~/.local/bin to your PATH." ;; esac
