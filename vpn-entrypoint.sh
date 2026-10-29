#!/bin/sh
# Gluetun only accepts an IP in the WireGuard Endpoint. The user's config may
# name a host, so a rewritten copy with the resolved IP is what gluetun reads.
# The mounted original is never modified.
set -eu
source=/vpn/credentials.conf
target=/gluetun/wireguard/wg0.conf
mkdir -p "$(dirname "$target")"

endpoint=$(sed -n 's/^[[:space:]]*Endpoint[[:space:]]*=[[:space:]]*//p' "$source" | head -n1 | tr -d '\r[:space:]')
host=${endpoint%:*}
port=${endpoint##*:}

case "$host" in
  *[!0-9.]*)
    ip=$(nslookup "$host" 2>/dev/null | awk '/^Name:/ {found=1} found && /^Address/ {print $NF}' \
      | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' | head -n1 || true)
    if [ -z "$ip" ]; then
      echo "Nao foi possivel resolver o endpoint da VPN ($host)." >&2
      exit 1
    fi
    echo "Endpoint da VPN $host resolvido para $ip"
    sed "s|^\([[:space:]]*Endpoint[[:space:]]*=[[:space:]]*\).*|\1$ip:$port|" "$source" > "$target"
    ;;
  *)
    cp "$source" "$target"
    ;;
esac
chmod 600 "$target"
exec /gluetun-entrypoint "$@"
