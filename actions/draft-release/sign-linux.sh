#!/usr/bin/env bash
# Signs a release's Linux packages in DIR with the key $SIGNING_FPR in $GNUPGHOME, before the
# checksums are computed (the embedded signatures change the rpm and deb files):
#
#   *.rpm                 embedded signature (rpmsign), checked by `rpm -K` and dnf's gpgcheck
#   *.deb                 embedded _gpgorigin member (debsigs), checked by debsig-verify
#   *.pkg.tar.zst         <file>.sig, the detached signature pacman looks for
#   other *-linux-* files <file>.asc (AppImage, tar.gz, flatpak, deb, zsync)
#   *.oci-manifest.json   <base>.oci.sig, the containers/image signature Flatpak checks for
#                         $OCI_REGISTRY@<manifest digest> (served from the remote's
#                         signature lookaside, <lookaside>/<repository>@sha256=<hex>/signature-1)
#
# It also writes release-signing-key.asc and verifies every signature before it returns.
#
# Usage: sign-linux.sh DIR
set -euo pipefail

dir="${1:?usage: sign-linux.sh DIR}"
: "${SIGNING_FPR:?}" "${GNUPGHOME:?}"
cd "$dir"
shopt -s nullglob

verify_home="$(mktemp -d)"
trap 'rm -rf -- "$verify_home"' EXIT
gpg --batch --armor --export "$SIGNING_FPR" >release-signing-key.asc
# Verification only trusts the exported public key, never the signing keyring.
gpg --homedir "$verify_home" --batch --quiet --import release-signing-key.asc
gpgv_keyring="$verify_home/trusted.gpg"
gpg --homedir "$verify_home" --batch --export >"$gpgv_keyring"
check() { gpgv --quiet --keyring "$gpgv_keyring" "$@" 2>/dev/null || { echo "::error::bad signature: $1" >&2; exit 1; }; }

rpms=(*.rpm)
if [ "${#rpms[@]}" -gt 0 ]; then
  # Ubuntu's rpm macros name /usr/bin/gpg2, which GnuPG 2.2+ no longer installs.
  rpmsign --resign --define "__gpg $(command -v gpg)" --define "_gpg_name $SIGNING_FPR" --define "_gpg_path $GNUPGHOME" \
    "${rpms[@]}" >/dev/null
  rpm --dbpath "$verify_home/rpmdb" --initdb
  rpmkeys --dbpath "$verify_home/rpmdb" --import release-signing-key.asc
  for f in "${rpms[@]}"; do
    rpmkeys --dbpath "$verify_home/rpmdb" --checksig "$f" | grep -q 'signatures OK' || { echo "::error::bad signature: $f" >&2; exit 1; }
  done
fi

for f in *.deb; do
  debsigs --sign=origin --default-key="$SIGNING_FPR" "$f"
  # debsigs signs the concatenated debian-binary, control and data members, in that order.
  members="$(ar t "$f" | grep -v '^_gpg')"
  # shellcheck disable=SC2086 # one member name per word
  ar p "$f" $members | gpgv --quiet --keyring "$gpgv_keyring" <(ar p "$f" _gpgorigin) - 2>/dev/null \
    || { echo "::error::bad signature: $f (_gpgorigin)" >&2; exit 1; }
done

for f in *-linux-*; do
  case "$f" in
    *.asc | *.sig | *.oci-manifest.json) ;;
    *.pkg.tar.zst)
      gpg --batch --yes --local-user "$SIGNING_FPR" --detach-sign -o "$f.sig" "$f"
      check "$f.sig" "$f"
      ;;
    *)
      gpg --batch --yes --local-user "$SIGNING_FPR" --armor --detach-sign -o "$f.asc" "$f"
      check "$f.asc" "$f"
      ;;
  esac
done

for f in *.oci-manifest.json; do
  sig="${f%.oci-manifest.json}.oci.sig"
  skopeo standalone-sign "$f" "${OCI_REGISTRY:?}" "$SIGNING_FPR" -o "$sig"
  GNUPGHOME="$verify_home" skopeo standalone-verify "$f" "$OCI_REGISTRY" "$SIGNING_FPR" "$sig" >/dev/null \
    || { echo "::error::bad signature: $sig" >&2; exit 1; }
done

echo "Signed the Linux packages with $SIGNING_FPR"
