# Stable PhotoCraft Linux OpenPGP fixture

Public, already-published bytes from the PhotoCraft v0.23.1 stable release:
`release-signing-key.asc` (ArtCraft Linux Release Signing Key, primary
2B76741B414AAD6CD33E631213A005C0B9D7C3ED, signing subkey
568DC1F68F8FAECC797F3C7B006DAF6BB0EF334E), `SHA256SUMS.txt` and its detached
`SHA256SUMS.txt.asc`. No secret material. Tests use them to show the nightly
verifier accepts the real stable key shape (certify-only primary, separate
signing subkey) and binds the signature to the primary fingerprint.
