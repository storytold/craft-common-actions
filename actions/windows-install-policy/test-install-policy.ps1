<#
.SYNOPSIS
  Build non-executable installer metadata fixtures; never install them.
#>
param(
  [Parameter(Mandatory = $true)] [string] $OutputDirectory,
  [Parameter(Mandatory = $true)] [string] $PackagingDirectory,
  [ValidateSet('x64','x86','arm64')] [string[]] $Architectures = @('x64','x86','arm64')
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$packaging = (Resolve-Path -LiteralPath $PackagingDirectory).Path
$root = (Resolve-Path -LiteralPath (Join-Path $packaging '..\..')).Path
$policyPath = Join-Path $packaging 'install-policy.json'
$policy = Get-Content -Raw -LiteralPath $policyPath | ConvertFrom-Json
# Strict mode turns a missing key into "property cannot be found"; name the file and the key instead.
function Get-PolicyList([string] $Name, [switch] $Required) {
  $property = $policy.PSObject.Properties[$Name]
  if ($null -eq $property -or $null -eq $property.Value) {
    if ($Required) { throw "The policy file '$policyPath' does not define '$Name'." }
    return @()
  }
  @($property.Value)
}
foreach ($key in 'id', 'icon') {
  if ($null -eq $policy.PSObject.Properties[$key] -or [string]::IsNullOrWhiteSpace([string]$policy.PSObject.Properties[$key].Value)) {
    throw "The policy file '$policyPath' does not define '$key'."
  }
}
[void](New-Item -ItemType Directory -Force -Path $OutputDirectory)
$output = (Resolve-Path -LiteralPath $OutputDirectory).Path
# Keep the app's legacy-source regression check alongside its source transformation.
$legacyCheck = Join-Path $packaging 'test-legacy-fixture.ps1'
if (Test-Path -LiteralPath $legacyCheck) {
  & $legacyCheck -Work (Join-Path $output 'legacy-source')
}
# Read every list the build needs before any Windows-only setup, so a bad policy fails first.
$sources = @(Get-PolicyList 'sources' -Required | ForEach-Object { Join-Path $packaging $_ })
$extensions = @(Get-PolicyList 'extensions' | ForEach-Object { '-ext'; "WixToolset.$_.wixext" })
$localization = @(Get-PolicyList 'localization' | ForEach-Object { '-loc'; Join-Path $packaging $_ })
$payloads = @(Get-PolicyList 'payloads')
Add-Type -AssemblyName System.Drawing
foreach ($arch in $Architectures) {
  $stage = Join-Path $output $arch
  [void](New-Item -ItemType Directory -Force -Path $stage)
  foreach ($name in $payloads) {
    $destination = Join-Path $stage $name
    [void](New-Item -ItemType Directory -Force -Path ([IO.Path]::GetDirectoryName($destination)))
    Set-Content -LiteralPath $destination -Encoding Ascii -Value 'NON-EXECUTABLE MSI METADATA TEST FIXTURE'
  }
  foreach ($folder in @('docs','models','notices')) {
    $path = Join-Path $stage $folder
    [void](New-Item -ItemType Directory -Force -Path $path)
    Set-Content -LiteralPath (Join-Path $path "$folder-fixture.txt") -Value 'Metadata test fixture, not a user file or model.'
  }
  foreach ($model in @('metadata-one.rten','metadata-two.rten')) {
    Set-Content -LiteralPath (Join-Path (Join-Path $stage 'models') $model) -Encoding Ascii -Value 'NON-EXECUTABLE MODEL METADATA FIXTURE'
  }
  foreach ($spec in @(@('banner.bmp',493,58),@('dialog.bmp',493,312))) {
    $bitmap = [Drawing.Bitmap]::new($spec[1],$spec[2],[Drawing.Imaging.PixelFormat]::Format24bppRgb)
    try { $bitmap.Save((Join-Path $stage $spec[0]),[Drawing.Imaging.ImageFormat]::Bmp) }
    finally { $bitmap.Dispose() }
  }
  Set-Content -LiteralPath (Join-Path $stage 'licence.rtf') -Encoding Ascii -Value '{\rtf1 Metadata test fixture.}'
  $msi = Join-Path $stage "$($policy.id)-NOT-INSTALLABLE.msi"
  & wix build @sources -arch $arch @extensions `
    -culture en-US -d 'Version=0.0.1' -d 'DisplayVersion=0.0.1-metadata-test' `
    -d "BinDir=$stage" -d "IconPath=$(Join-Path $root $policy.icon)" `
    -d "DocsDir=$(Join-Path $stage 'docs')" -d "ModelsDir=$(Join-Path $stage 'models')" `
    -d "AppNoticesDir=$(Join-Path $stage 'notices')" -d 'HasCraftFontLicences=1' -d 'HasCli=1' `
    -d "BannerBmp=$(Join-Path $stage 'banner.bmp')" -d "DialogBmp=$(Join-Path $stage 'dialog.bmp')" `
    -d "UiBannerBmp=$(Join-Path $stage 'banner.bmp')" -d "UiDialogBmp=$(Join-Path $stage 'dialog.bmp')" `
    -d "LicenseRtf=$(Join-Path $stage 'licence.rtf')" -o $msi @localization
  if ($LASTEXITCODE -ne 0) { throw "WiX validation failed for $arch (exit $LASTEXITCODE)." }
  $shell = (Get-Process -Id $PID).Path
  & $shell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'check-install-policy.ps1') -MsiPath $msi -PolicyPath $policyPath
  if ($LASTEXITCODE -ne 0) { throw "Compiled installer policy failed for $arch (exit $LASTEXITCODE)." }
  Write-Output "PASS $arch metadata-only package. This is not an executable installer."
}
