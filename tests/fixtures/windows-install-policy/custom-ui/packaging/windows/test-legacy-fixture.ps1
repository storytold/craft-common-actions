<# An app's optional regression hook: test-install-policy.ps1 runs it before building the fixtures.
   This one only records that it ran, so the test workflow can check the hook is called. #>
param([Parameter(Mandatory = $true)] [string] $Work)
$ErrorActionPreference = 'Stop'
[void](New-Item -ItemType Directory -Force -Path $Work)
Set-Content -LiteralPath (Join-Path $Work 'legacy-hook-ran.txt') -Value 'test-legacy-fixture.ps1 ran'
Write-Output 'PASS legacy hook ran.'
