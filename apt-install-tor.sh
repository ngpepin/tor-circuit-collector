#!/usr/bin/env bash
set -Eeuo pipefail

# Install the latest stable Tor packages from Tor Project's official APT
# repository, then hold Tor and its GeoIP database at the installed version.
# Re-running this script refreshes repository metadata, temporarily removes the
# holds when a newer stable release exists, replaces stale packages, and holds
# the newly installed versions again.

readonly TOR_REPOSITORY="https://deb.torproject.org/torproject.org/"
readonly TOR_KEY_URL="${TOR_REPOSITORY}A3C4F0F979CAA22CDBA8F512EE8CBC9E886DDD89.asc"
readonly TOR_KEY_FINGERPRINT="A3C4F0F979CAA22CDBA8F512EE8CBC9E886DDD89"
readonly TOR_KEYRING="/usr/share/keyrings/deb.torproject.org-keyring.gpg"
readonly TOR_SOURCE_FILE="/etc/apt/sources.list.d/tor.sources"
readonly -a TOR_PACKAGES=(tor tor-geoipdb)

log() {
  printf '[apt-install-tor] %s\n' "$*"
}

die() {
  printf '[apt-install-tor] Error: %s\n' "$*" >&2
  exit 1
}

for command_name in apt-cache apt-get apt-mark awk cat dpkg dpkg-query grep install mkdir mktemp rm; do
  command -v "$command_name" >/dev/null 2>&1 || die "Required command not found: $command_name"
done

if ((EUID == 0)); then
  AS_ROOT=()
else
  command -v sudo >/dev/null 2>&1 || die "Run this script as root or install sudo."
  AS_ROOT=(sudo)
  "${AS_ROOT[@]}" -v
fi

run_root() {
  "${AS_ROOT[@]}" "$@"
}

export DEBIAN_FRONTEND=noninteractive

if [[ ! -r /etc/os-release ]]; then
  die "This script requires a Debian or Ubuntu-family system with /etc/os-release."
fi

# shellcheck disable=SC1091
source /etc/os-release
case "${ID:-}:${ID_LIKE:-}" in
  debian:* | ubuntu:* | *:debian* | *:ubuntu*) ;;
  *) die "Unsupported distribution: ${PRETTY_NAME:-unknown}. This script requires APT on Debian or Ubuntu." ;;
esac

distribution="${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}"
[[ -n "$distribution" ]] || die "Could not determine the distribution codename."

architecture="$(dpkg --print-architecture)"
case "$architecture" in
  amd64 | arm64) ;;
  *) die "Tor Project's APT repository does not provide packages for architecture: $architecture" ;;
esac

missing_prerequisites=()
command -v curl >/dev/null 2>&1 || missing_prerequisites+=(curl)
command -v gpg >/dev/null 2>&1 || missing_prerequisites+=(gnupg)
[[ -r /etc/ssl/certs/ca-certificates.crt ]] || missing_prerequisites+=(ca-certificates)

if ((${#missing_prerequisites[@]})); then
  log "Installing prerequisites: ${missing_prerequisites[*]}"
  run_root apt-get update
  run_root apt-get install -y "${missing_prerequisites[@]}"
fi

temp_dir="$(mktemp -d /tmp/apt-install-tor.XXXXXX)"
cleanup() {
  if [[ -n "${temp_dir:-}" && "$temp_dir" == /tmp/apt-install-tor.* && -d "$temp_dir" ]]; then
    rm -rf -- "$temp_dir"
  fi
}
trap cleanup EXIT

key_ascii="$temp_dir/tor-project.asc"
key_binary="$temp_dir/tor-project.gpg"
gpg_home="$temp_dir/gnupg"
mkdir -m 0700 "$gpg_home"

log "Downloading and verifying the Tor Project repository key."
curl --fail --silent --show-error --location "$TOR_KEY_URL" --output "$key_ascii"
downloaded_fingerprint="$(
  gpg --batch --homedir "$gpg_home" --show-keys --with-colons "$key_ascii" |
    awk -F: '$1 == "fpr" { print toupper($10); exit }'
)"
[[ "$downloaded_fingerprint" == "$TOR_KEY_FINGERPRINT" ]] ||
  die "Repository key fingerprint mismatch (received ${downloaded_fingerprint:-none})."

gpg --batch --yes --homedir "$gpg_home" --dearmor --output "$key_binary" "$key_ascii"
run_root install -d -o root -g root -m 0755 /usr/share/keyrings /etc/apt/sources.list.d
run_root install -o root -g root -m 0644 "$key_binary" "$TOR_KEYRING"

source_definition="$temp_dir/tor.sources"
cat >"$source_definition" <<EOF
Types: deb deb-src
URIs: $TOR_REPOSITORY
Suites: $distribution
Components: main
Architectures: $architecture
Signed-By: $TOR_KEYRING
EOF
run_root install -o root -g root -m 0644 "$source_definition" "$TOR_SOURCE_FILE"

log "Refreshing APT metadata for Tor Project's $distribution repository."
run_root apt-get update

latest_official_version() {
  local package_name="$1"
  apt-cache madison "$package_name" |
    awk -F'|' '$3 ~ /deb\.torproject\.org\/torproject\.org/ {
      version=$2
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", version)
      print version
      exit
    }'
}

installed_version() {
  local package_name="$1"
  local package_state
  package_state="$(dpkg-query -W -f='${db:Status-Status} ${Version}' "$package_name" 2>/dev/null || true)"
  if [[ "$package_state" == installed\ * ]]; then
    printf '%s\n' "${package_state#installed }"
  fi
}

latest_tor="$(latest_official_version tor)"
latest_geoip="$(latest_official_version tor-geoipdb)"
[[ -n "$latest_tor" ]] || die "No official Tor package was found for $distribution/$architecture."
[[ -n "$latest_geoip" ]] || die "No official tor-geoipdb package was found for $distribution/$architecture."

installed_tor="$(installed_version tor)"
installed_geoip="$(installed_version tor-geoipdb)"

log "Tor: installed=${installed_tor:-none}, latest-official=$latest_tor"
log "GeoIP: installed=${installed_geoip:-none}, latest-official=$latest_geoip"

if [[ "$installed_tor" == "$latest_tor" && "$installed_geoip" == "$latest_geoip" ]]; then
  log "Latest Tor and GeoIP packages are already installed."
else
  installed_packages=()
  outdated_packages=()

  [[ -n "$installed_tor" ]] && installed_packages+=(tor)
  [[ -n "$installed_geoip" ]] && installed_packages+=(tor-geoipdb)
  [[ -n "$installed_tor" && "$installed_tor" != "$latest_tor" ]] && outdated_packages+=(tor)
  [[ -n "$installed_geoip" && "$installed_geoip" != "$latest_geoip" ]] && outdated_packages+=(tor-geoipdb)

  if ((${#installed_packages[@]})); then
    log "Temporarily removing package holds."
    run_root apt-mark unhold "${installed_packages[@]}" >/dev/null
  fi

  if ((${#outdated_packages[@]})); then
    log "Removing outdated packages: ${outdated_packages[*]}"
    # This is a package removal, not a purge: existing torrc configuration and
    # Tor state are retained for the replacement installation.
    run_root apt-get remove -y "${outdated_packages[@]}"
  fi

  log "Installing Tor $latest_tor and GeoIP $latest_geoip."
  run_root apt-get install -y --allow-downgrades \
    "tor=$latest_tor" \
    "tor-geoipdb=$latest_geoip" \
    deb.torproject.org-keyring
fi

installed_tor="$(installed_version tor)"
installed_geoip="$(installed_version tor-geoipdb)"
[[ "$installed_tor" == "$latest_tor" ]] || die "Tor installation verification failed."
[[ "$installed_geoip" == "$latest_geoip" ]] || die "GeoIP installation verification failed."

run_root apt-mark hold "${TOR_PACKAGES[@]}" >/dev/null

held_packages="$(apt-mark showhold)"
for package_name in "${TOR_PACKAGES[@]}"; do
  grep -Fxq "$package_name" <<<"$held_packages" || die "Failed to hold $package_name."
done

log "Installed and held tor=$installed_tor and tor-geoipdb=$installed_geoip."
log "Re-run this script whenever you want to check for and install a newer stable release."
