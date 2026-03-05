#!/bin/bash
# One-time setup: copy your SSH key to the server so deploy never asks for a password.
# Run from your Mac (project root): ./deploy/ssh-setup.sh
# You will be asked for the server password ONCE; after that, ./deploy/update-server.sh will not ask.

SERVER="${1:-root@207.180.212.142}"

if ! command -v ssh-copy-id &>/dev/null; then
  echo "ssh-copy-id not found. On Mac you can: brew install ssh-copy-id"
  echo "Or manually append your public key to the server:"
  echo "  cat ~/.ssh/id_rsa.pub   # or id_ed25519.pub"
  echo "  Then on server: echo 'PASTE_KEY' >> ~/.ssh/authorized_keys"
  exit 1
fi

KEY=""
for f in ~/.ssh/id_ed25519.pub ~/.ssh/id_rsa.pub; do
  if [ -f "$f" ]; then
    KEY="$f"
    break
  fi
done

if [ -z "$KEY" ]; then
  echo "No SSH public key found. Generate one first:"
  echo "  ssh-keygen -t ed25519 -C 'your@email' -f ~/.ssh/id_ed25519 -N ''"
  exit 1
fi

echo "Using key: $KEY"
echo "You will be asked for the server password once."
ssh-copy-id -i "$KEY" "$SERVER" && echo "Done. Future deploys will not ask for a password."
