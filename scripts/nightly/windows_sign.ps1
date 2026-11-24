# Signs or verifies the nightly GUI in an unpacked launcher payload. The payload is data: only
# the app's checked-out packaging/windows/sign.ps1 runs. Every expected value comes from the
# nightly registry (actions/nightly-feed/apps.json) through scripts/nightly/platforms.py.
param(
  [Parameter(Mandatory=$true)][ValidateSet('sign','verify')][string]$Mode,
  [Parameter(Mandatory=$true)][string]$Root,
  [Parameter(Mandatory=$true)][string]$Executable,
  [Parameter(Mandatory=$true)][string]$ExpectedProductName,
  [Parameter(Mandatory=$true)][string]$ExpectedVersion,
  [Parameter(Mandatory=$true)][string]$ExpectedPublisher,
  [Parameter(Mandatory=$true)][string]$ExpectedRoot,
  [Parameter(Mandatory=$true)][string]$ExpectedEkus,
  [string]$Signer
)
$ErrorActionPreference = 'Stop'
foreach ($value in $Executable, $ExpectedProductName, $ExpectedVersion, $ExpectedPublisher, $ExpectedRoot, $ExpectedEkus) {
  if (-not $value -or $value.Contains("`n") -or $value.Contains("`r")) { throw 'expected values must be single non-empty lines' }
}
if ($Executable -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]*\.exe$') { throw 'the GUI must be a top-level .exe' }
$Exe = Join-Path (Resolve-Path -LiteralPath $Root).Path $Executable
if ($Mode -eq 'sign') {
  if (-not $Signer) { throw 'sign mode needs the app signer script' }
  if ($env:WINDOWS_CERTIFICATE) {
    if (-not $env:WINDOWS_CERTIFICATE_PASSWORD) { throw 'complete PFX pair required' }
  } else {
    foreach ($name in 'AZURE_TENANT_ID','AZURE_CLIENT_ID','AZURE_CLIENT_SECRET','AZURE_SIGNING_ENDPOINT','AZURE_SIGNING_ACCOUNT','AZURE_CERT_PROFILE') {
      if (-not [Environment]::GetEnvironmentVariable($name)) { throw "required signing configuration missing: $name" }
    }
  }
  & $Signer -Required -CheckOnly | Out-Null
  if ($LASTEXITCODE -ne 0) { throw 'required app signing configuration preflight failed' }
  & $Signer -Required -Files @($Exe) | Out-Null
  if ($LASTEXITCODE -ne 0) { throw 'app signing backend failed' }
}
$Version = [Diagnostics.FileVersionInfo]::GetVersionInfo($Exe)
if ($Version.ProductName -cne $ExpectedProductName -or $Version.ProductVersion -cne $ExpectedVersion) {
  throw 'the PE lacks the exact nightly ProductName and ProductVersion'
}
$Signature = Get-AuthenticodeSignature -LiteralPath $Exe
if ($Signature.Status -ne 'Valid' -or -not $Signature.SignerCertificate -or -not $Signature.TimeStamperCertificate) {
  throw 'valid timestamped Authenticode signature required'
}
$Certificate = $Signature.SignerCertificate
# craft-launcher's Windows trust (nightly_trust.rs): exact subject, chain root and every EKU. The
# leaf and issuing CA rotate, so neither is pinned.
if ($Certificate.Subject -cne $ExpectedPublisher) { throw 'Authenticode signer differs from the registry publisher subject' }
$Chain = New-Object Security.Cryptography.X509Certificates.X509Chain
# Get-AuthenticodeSignature already checked revocation; the chain here only names the root.
$Chain.ChainPolicy.RevocationMode = [Security.Cryptography.X509Certificates.X509RevocationMode]::NoCheck
if (-not $Chain.Build($Certificate)) { throw 'the signer certificate chain does not build' }
$Top = $Chain.ChainElements[$Chain.ChainElements.Count - 1].Certificate
if ($Top.Subject -cne $ExpectedRoot) { throw 'the signer chain ends at another root than the registry names' }
$Usages = @($Certificate.Extensions | Where-Object { $_ -is [Security.Cryptography.X509Certificates.X509EnhancedKeyUsageExtension] } |
  ForEach-Object { $_.EnhancedKeyUsages } | ForEach-Object { $_.Value })
foreach ($eku in $ExpectedEkus.Split(',')) {
  if ($Usages -cnotcontains $eku) { throw "the signer certificate lacks EKU $eku" }
}
Write-Output $Certificate.Subject
