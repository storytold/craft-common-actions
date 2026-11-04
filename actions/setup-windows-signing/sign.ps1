<#
.SYNOPSIS
  Authenticode-sign files with signtool, using whichever signing material is configured.

.DESCRIPTION
  Tries, in order:
    1. A code-signing certificate file:   WINDOWS_CERTIFICATE (base64 .pfx), WINDOWS_CERTIFICATE_PASSWORD
    2. Azure Trusted Signing:             AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET,
                                          AZURE_SIGNING_ENDPOINT (e.g. https://eus.codesigning.azure.net),
                                          AZURE_SIGNING_ACCOUNT, AZURE_CERT_PROFILE
  With neither, it prints a warning and leaves the files unsigned (exit code 0), so test builds
  still produce installers.

  The description and URL embedded in the signature come from -ProductName and -ProductUrl, or from
  CRAFT_SIGN_PRODUCT and CRAFT_SIGN_URL, which the setup-windows-signing action exports.

  This is the one place to adapt when the signing setup changes (an HSM/cloud key, a different
  timestamp server, ...). Optional overrides: WINDOWS_TIMESTAMP_URL, SIGNTOOL (path to signtool.exe).

.EXAMPLE
  pwsh $env:CRAFT_SIGN_SCRIPT dist/app.exe dist/App.msi
#>
[CmdletBinding(PositionalBinding = $false)]
param(
  [string] $ProductName = $env:CRAFT_SIGN_PRODUCT,
  [string] $ProductUrl = $env:CRAFT_SIGN_URL,
  [Parameter(Mandatory = $true, ValueFromRemainingArguments = $true)]
  [string[]] $Files
)
$ErrorActionPreference = 'Stop'

# Azure Trusted Signing loads this NuGet package's dlib into signtool, beside the signing secrets,
# so the package is pinned to a reviewed version and SHA-256 and refused on any mismatch.
# Upgrading is a deliberate change to both values: download
# https://www.nuget.org/api/v2/package/Microsoft.Trusted.Signing.Client/<version> and record
# (Get-FileHash -Algorithm SHA256 <file>).Hash.
$TrustedSigningClient = @{
  Version = '1.0.95'
  Sha256  = '3BFCF1E0A3CB42AF1692F0A8ED45C15DE070C2DE86F28A59B2795D904D8A920F'
}

function Write-Warn([string] $Message) {
  if ($env:GITHUB_ACTIONS) { Write-Output "::warning::$Message" } else { Write-Warning $Message }
}

function Find-SignTool {
  if ($env:SIGNTOOL) { return $env:SIGNTOOL }
  $onPath = Get-Command signtool.exe -ErrorAction SilentlyContinue
  if ($onPath) { return $onPath.Source }
  $kits = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10\bin'
  $found = Get-ChildItem -Path $kits -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -match '\\x64\\signtool\.exe$' } |
    Sort-Object FullName -Descending | Select-Object -First 1
  if (-not $found) { throw 'signtool.exe not found (install the Windows SDK)' }
  return $found.FullName
}

function Invoke-SignTool([string[]] $Arguments) {
  & $script:SignTool @Arguments
  if ($LASTEXITCODE -ne 0) { throw "signtool $($Arguments[0]) failed with exit code $LASTEXITCODE" }
}

if (-not $ProductName) { throw 'No product name: pass -ProductName or set CRAFT_SIGN_PRODUCT (see the setup-windows-signing action).' }
if (-not $ProductUrl) { throw 'No product URL: pass -ProductUrl or set CRAFT_SIGN_URL (see the setup-windows-signing action).' }

$Files = $Files | ForEach-Object { (Resolve-Path $_).Path }
$haveCert = [bool]$env:WINDOWS_CERTIFICATE
$azureVars = 'AZURE_TENANT_ID', 'AZURE_CLIENT_ID', 'AZURE_CLIENT_SECRET', 'AZURE_SIGNING_ENDPOINT', 'AZURE_SIGNING_ACCOUNT', 'AZURE_CERT_PROFILE'
$haveAzure = -not ($azureVars | Where-Object { -not [Environment]::GetEnvironmentVariable($_) })

if (-not $haveCert -and -not $haveAzure) {
  Write-Warn "Windows signing secrets not set (WINDOWS_CERTIFICATE or AZURE_*): leaving unsigned: $($Files -join ', ')"
  exit 0
}

$script:SignTool = Find-SignTool
$common = @('sign', '/v', '/fd', 'SHA256', '/td', 'SHA256', '/d', $ProductName, '/du', $ProductUrl)
# A fresh directory with an unpredictable name: the certificate and signing metadata are written here.
$tmp = Join-Path ([IO.Path]::GetTempPath()) "craft-sign-$([IO.Path]::GetRandomFileName())"
New-Item -ItemType Directory -Path $tmp | Out-Null

try {
  if ($haveCert) {
    Write-Output "Signing with WINDOWS_CERTIFICATE: $($Files -join ', ')"
    $pfx = Join-Path $tmp 'cert.pfx'
    [IO.File]::WriteAllBytes($pfx, [Convert]::FromBase64String($env:WINDOWS_CERTIFICATE))
    $ts = if ($env:WINDOWS_TIMESTAMP_URL) { $env:WINDOWS_TIMESTAMP_URL } else { 'http://timestamp.digicert.com' }
    $signArgs = $common + @('/tr', $ts, '/f', $pfx)
    if ($env:WINDOWS_CERTIFICATE_PASSWORD) { $signArgs += @('/p', $env:WINDOWS_CERTIFICATE_PASSWORD) }
    Invoke-SignTool ($signArgs + $Files)
  }
  else {
    Write-Output "Signing with Azure Trusted Signing ($env:AZURE_SIGNING_ACCOUNT / $env:AZURE_CERT_PROFILE): $($Files -join ', ')"
    # The dlib authenticates with DefaultAzureCredential, which reads AZURE_TENANT_ID,
    # AZURE_CLIENT_ID and AZURE_CLIENT_SECRET from the environment.
    $pkg = Join-Path $tmp 'trusted-signing.zip'
    $version = $TrustedSigningClient.Version
    Invoke-WebRequest -Uri "https://www.nuget.org/api/v2/package/Microsoft.Trusted.Signing.Client/$version" -OutFile $pkg
    $sha256 = (Get-FileHash -Algorithm SHA256 -Path $pkg).Hash
    if ($sha256 -ne $TrustedSigningClient.Sha256) {
      throw "Microsoft.Trusted.Signing.Client $version has SHA-256 $sha256, expected $($TrustedSigningClient.Sha256): refusing to load it"
    }
    Expand-Archive -Path $pkg -DestinationPath (Join-Path $tmp 'client') -Force
    $dlib = Get-ChildItem -Path (Join-Path $tmp 'client') -Recurse -Filter Azure.CodeSigning.Dlib.dll |
      Where-Object { $_.FullName -match '\\x64\\' } | Select-Object -First 1
    if (-not $dlib) { throw 'Azure.CodeSigning.Dlib.dll not found in Microsoft.Trusted.Signing.Client' }
    $metadata = Join-Path $tmp 'metadata.json'
    @{
      Endpoint               = $env:AZURE_SIGNING_ENDPOINT
      CodeSigningAccountName = $env:AZURE_SIGNING_ACCOUNT
      CertificateProfileName = $env:AZURE_CERT_PROFILE
    } | ConvertTo-Json | Set-Content -Path $metadata -Encoding utf8
    $ts = if ($env:WINDOWS_TIMESTAMP_URL) { $env:WINDOWS_TIMESTAMP_URL } else { 'http://timestamp.acs.microsoft.com' }
    Invoke-SignTool ($common + @('/tr', $ts, '/dlib', $dlib.FullName, '/dmdf', $metadata) + $Files)
  }
  Invoke-SignTool (@('verify', '/pa', '/v') + $Files)
}
finally {
  Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}
