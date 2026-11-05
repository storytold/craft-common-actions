<#
.SYNOPSIS
  Build non-executable installer metadata fixtures from the app's WiX sources; never install them.
.DESCRIPTION
  Every build gets Version, DisplayVersion, BinDir (the policy's payloads, as placeholder text files)
  and IconPath. Anything else the app's WiX reads comes from the policy's wixVariables: a literal
  value, or a placeholder this script creates and passes the path of:
    "@folder [name ...]"  a folder holding the named placeholder files (default one .txt)
    "@bitmap WxH"         a blank 24-bit bitmap of that size
    "@rtf"                a minimal RTF document
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
function Get-PolicyValue([string] $Name, [switch] $Required) {
  $property = $policy.PSObject.Properties[$Name]
  if ($null -eq $property -or $null -eq $property.Value -or [string]::IsNullOrWhiteSpace([string]$property.Value)) {
    if ($Required) { throw "The policy file '$policyPath' does not define '$Name'." }
    return $null
  }
  $property.Value
}
$id = Get-PolicyValue 'id' -Required
$icon = Join-Path $root (Get-PolicyValue 'icon' -Required)
$sources = @(Get-PolicyValue 'sources' -Required | ForEach-Object { Join-Path $packaging $_ })
$extensions = @(Get-PolicyValue 'extensions' | Where-Object { $_ } | ForEach-Object { '-ext'; "WixToolset.$_.wixext" })
$localization = @(Get-PolicyValue 'localization' | Where-Object { $_ } | ForEach-Object { '-loc'; Join-Path $packaging $_ })
$payloads = @(Get-PolicyValue 'payloads' | Where-Object { $_ })
$variables = Get-PolicyValue 'wixVariables'
$variables = if ($variables) { @($variables.PSObject.Properties) } else { @() }
foreach ($variable in $variables) {
  if ($variable.Name -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { throw "wixVariables: '$($variable.Name)' is not a WiX variable name." }
}

function New-Placeholder([string] $Stage, [string] $Name, [string] $Spec) {
  $words = @($Spec.Substring(1) -split '\s+' | Where-Object { $_ })
  switch ($words[0]) {
    'folder' {
      $folder = Join-Path $Stage $Name
      [void](New-Item -ItemType Directory -Force -Path $folder)
      $files = if ($words.Count -gt 1) { $words[1..($words.Count - 1)] } else { @("$Name-placeholder.txt") }
      foreach ($file in $files) { Set-Content -LiteralPath (Join-Path $folder $file) -Encoding Ascii -Value 'NON-EXECUTABLE MSI METADATA TEST FIXTURE' }
      return $folder
    }
    'bitmap' {
      if ($words.Count -ne 2 -or $words[1] -notmatch '^(\d+)x(\d+)$') { throw "wixVariables: $Name needs '@bitmap WIDTHxHEIGHT'." }
      $path = Join-Path $Stage "$Name.bmp"
      $bitmap = [Drawing.Bitmap]::new([int]$Matches[1], [int]$Matches[2], [Drawing.Imaging.PixelFormat]::Format24bppRgb)
      try { $bitmap.Save($path, [Drawing.Imaging.ImageFormat]::Bmp) } finally { $bitmap.Dispose() }
      return $path
    }
    'rtf' {
      $path = Join-Path $Stage "$Name.rtf"
      Set-Content -LiteralPath $path -Encoding Ascii -Value '{\rtf1 Metadata test fixture.}'
      return $path
    }
    default { throw "wixVariables: $Name has an unknown placeholder '$Spec' (use @folder, @bitmap WxH or @rtf)." }
  }
}

[void](New-Item -ItemType Directory -Force -Path $OutputDirectory)
$output = (Resolve-Path -LiteralPath $OutputDirectory).Path
# The app's own regression hook, if it has one, runs before any build.
$hook = Join-Path $packaging 'test-legacy-fixture.ps1'
if (Test-Path -LiteralPath $hook) { & $hook -Work (Join-Path $output 'legacy-source') }

Add-Type -AssemblyName System.Drawing
$shell = (Get-Process -Id $PID).Path
foreach ($arch in $Architectures) {
  $stage = Join-Path $output $arch
  [void](New-Item -ItemType Directory -Force -Path $stage)
  foreach ($name in $payloads) {
    $destination = Join-Path $stage $name
    [void](New-Item -ItemType Directory -Force -Path ([IO.Path]::GetDirectoryName($destination)))
    Set-Content -LiteralPath $destination -Encoding Ascii -Value 'NON-EXECUTABLE MSI METADATA TEST FIXTURE'
  }
  $defines = @('-d', 'Version=0.0.1', '-d', 'DisplayVersion=0.0.1-metadata-test', '-d', "BinDir=$stage", '-d', "IconPath=$icon")
  foreach ($variable in $variables) {
    $value = [string]$variable.Value
    if ($value.StartsWith('@')) { $value = New-Placeholder $stage $variable.Name $value }
    $defines += @('-d', "$($variable.Name)=$value")
  }
  $msi = Join-Path $stage "$id-NOT-INSTALLABLE.msi"
  & wix build @sources -arch $arch @extensions -culture en-US @defines -o $msi @localization
  if ($LASTEXITCODE -ne 0) { throw "WiX validation failed for $arch (exit $LASTEXITCODE)." }
  & $shell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'check-install-policy.ps1') -MsiPath $msi -PolicyPath $policyPath
  if ($LASTEXITCODE -ne 0) { throw "Compiled installer policy failed for $arch (exit $LASTEXITCODE)." }
  Write-Output "PASS $arch metadata-only package. This is not an executable installer."
}
