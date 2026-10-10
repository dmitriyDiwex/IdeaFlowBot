#!/usr/bin/env bash
set -euo pipefail

# Restore the exact Google service account key supplied for this deployment.
# The decryption password is supplied separately and is never stored in Git.
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
command -v openssl >/dev/null || { echo "Install openssl first." >&2; exit 1; }
command -v sha256sum >/dev/null || { echo "sha256sum is required." >&2; exit 1; }

umask 077
mkdir -p data
temporary_key=$(mktemp data/.google-service-account.XXXXXX)
trap 'rm -f -- "$temporary_key"' EXIT

salt_options=()
openssl_help=$(openssl enc -help 2>&1 || true)
if [[ "$openssl_help" == *"-saltlen"* ]]; then
    salt_options=(-saltlen 8)
fi

IFS= read -r -s -p "Decryption password: " decryption_password
printf "\n"
printf '%s\n' "$decryption_password" | openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -md sha256 \
    "${salt_options[@]}" \
    -in deploy/google-service-account.json.enc -out "$temporary_key" -pass stdin
unset decryption_password
printf '%s  %s\n' 'd5b196c1f3fea36561aabe7114292450dd25cd2abf13153a203093266d50f13b' "$temporary_key" | sha256sum --check --status
chmod 600 "$temporary_key"
mv -- "$temporary_key" data/google-service-account.json
trap - EXIT
echo "Restored data/google-service-account.json. Original JSON verified."
