# Isolated signer. Only trusted checked-out scripts execute; payload is data.
param(
  [Parameter(Mandatory=$true)][ValidateSet('sign','verify')][string]$Mode,
  [Parameter(Mandatory=$true)][string]$Root,
  [Parameter(Mandatory=$true)][string]$ExpectedPublisher,
  [Parameter(Mandatory=$true)][string]$ExpectedVersion,
  # The app's GUI exe and nightly ProductName, from its registry row (scripts/nightly_app.py).
  [Parameter(Mandatory=$true)][ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]{0,127}[.]exe$')][string]$ExeName,
  [Parameter(Mandatory=$true)][ValidatePattern('^[A-Z][A-Za-z0-9]{0,31} Nightly$')][string]$ProductName,
  [string]$Signer
)
$ErrorActionPreference = 'Stop'
$Exe = Join-Path (Resolve-Path -LiteralPath $Root).Path $ExeName
if (-not $ExpectedPublisher -or $ExpectedPublisher.Contains("`n") -or $ExpectedPublisher.Contains("`r")) { throw 'exact publisher subject required' }
if ($Mode -eq 'sign') {
  if ($env:WINDOWS_CERTIFICATE) {
    if (-not $env:WINDOWS_CERTIFICATE_PASSWORD) { throw 'complete PFX pair required' }
  } else {
    foreach ($name in 'AZURE_TENANT_ID','AZURE_CLIENT_ID','AZURE_CLIENT_SECRET','AZURE_SIGNING_ENDPOINT','AZURE_SIGNING_ACCOUNT','AZURE_CERT_PROFILE') {
      if (-not [Environment]::GetEnvironmentVariable($name)) { throw "required signing configuration missing: $name" }
    }
  }
  # Stable optional sign.ps1 is guarded above and independently verified below.
  # Source path is workflow-owned, never read from an archive/descriptor.
  & $Signer -Required -CheckOnly | Out-Null
  if ($LASTEXITCODE -ne 0) { throw 'required app signing configuration preflight failed' }
  & $Signer -Required -Files @($Exe) | Out-Null
  if ($LASTEXITCODE -ne 0) { throw 'app signing backend failed' }
}
$Version = [Diagnostics.FileVersionInfo]::GetVersionInfo($Exe)
if ($Version.ProductName -cne $ProductName -or $Version.ProductVersion -cne $ExpectedVersion) { throw 'signed PE lost exact nightly ProductName/ProductVersion' }
$Signature = Get-AuthenticodeSignature -LiteralPath $Exe
if ($Signature.Status -ne 'Valid' -or -not $Signature.SignerCertificate -or -not $Signature.TimeStamperCertificate) { throw 'valid timestamped Authenticode signature required' }
if ($Signature.SignerCertificate.Subject -cne $ExpectedPublisher) { throw 'Authenticode signer differs from configured exact publisher subject' }
# Valid verifies chain/policy. Pin publisher identity, not rotating Azure leaf thumbprints.
Write-Output $Signature.SignerCertificate.Subject
