<#
.SYNOPSIS
  Install an MSI and start what it installed the way the winget-pkgs validator does:
  in the background, with no desktop. Fails when the app cannot survive that.

.DESCRIPTION
  winget-pkgs validation installs each new version on a VM and starts the installed
  programs without an interactive desktop. There, wgpu cannot configure a window surface
  ("Surface::configure: Invalid surface"). An app that panics (exit 101) or exits with an
  error on that is labelled Validation-Executable-Error and never reaches WinGet; an app
  that reports the error in a dialog and stays open passes (FilmCraft 0.13.0 -> 0.14.0).

  Each installed .exe is started as SYSTEM through a scheduled task, which gives it no
  desktop even on a runner that has one, and watched for -WaitSeconds.
    - the main app (-MainExe): FAIL if it exits with any non-zero code in that time.
    - every other .exe (CLIs, helpers): FAIL only on a crash (101 = Rust panic, or an
      NTSTATUS crash code such as 0xC0000005). A CLI printing usage and exiting 1 or 2
      is fine; the validator accepts that.
#>
param(
  [Parameter(Mandatory)] [string] $Msi,
  [Parameter(Mandatory)] [string] $MainExe,
  [int] $WaitSeconds = 30
)
$ErrorActionPreference = 'Stop'

function Write-Summary([string] $line) {
  Write-Output $line
  if ($env:GITHUB_STEP_SUMMARY) { Add-Content -LiteralPath $env:GITHUB_STEP_SUMMARY -Value $line }
}

function Get-InstalledExes {
  @("$env:LOCALAPPDATA\Programs", $env:ProgramFiles, ${env:ProgramFiles(x86)}) |
    Where-Object { $_ -and (Test-Path $_) } |
    ForEach-Object { Get-ChildItem -LiteralPath $_ -Recurse -Filter *.exe -ErrorAction SilentlyContinue } |
    ForEach-Object FullName
}

function Test-IsCrash([int] $code) {
  # 101: Rust panic. Negative values are NTSTATUS errors (0xC0000005 access violation,
  # 0xC0000409 stack buffer overrun, ...) seen as signed 32-bit exit codes.
  return ($code -eq 101 -or $code -lt 0)
}

# A path or a wildcard that matches exactly one MSI (release.yml passes dist/release/*-windows-x64.msi).
$found = @(Resolve-Path -Path $Msi -ErrorAction SilentlyContinue | ForEach-Object Path)
if ($found.Count -ne 1) { Write-Output "::error::'$Msi' must match exactly one MSI; it matches $($found.Count)"; exit 1 }
$Msi = $found[0]
$work = Join-Path ([IO.Path]::GetTempPath()) ("launch-check-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force $work | Out-Null

# Install the way the current manifests do: msiexec /qn with no extra properties.
$before = @(Get-InstalledExes)
$msiLog = Join-Path $work 'install.log'
$install = Start-Process msiexec.exe -ArgumentList @('/i', "`"$Msi`"", '/qn', '/norestart', '/l*v', "`"$msiLog`"") -Wait -PassThru
if ($install.ExitCode -ne 0 -and $install.ExitCode -ne 3010) {
  Get-Content $msiLog -Tail 40 -ErrorAction SilentlyContinue
  Write-Output "::error::msiexec failed with exit code $($install.ExitCode) installing $(Split-Path $Msi -Leaf)"
  exit 1
}
$exes = @(Get-InstalledExes | Where-Object { $before -notcontains $_ })
if (-not $exes) { Write-Output "::error::Installing $(Split-Path $Msi -Leaf) added no .exe"; exit 1 }
if (-not ($exes | Where-Object { (Split-Path $_ -Leaf) -ieq $MainExe })) {
  Write-Output "::error::$MainExe is not among the installed programs: $(($exes | ForEach-Object { Split-Path $_ -Leaf }) -join ', ')"
  exit 1
}

# The scheduled task runs this wrapper, which starts the exe and records how it ended.
$wrapper = Join-Path $work 'run-one.ps1'
Set-Content -LiteralPath $wrapper -Encoding utf8 -Value @'
param([string] $Exe, [string] $Out, [int] $WaitSeconds)
$base = [IO.Path]::Combine([IO.Path]::GetDirectoryName($Out), [IO.Path]::GetFileNameWithoutExtension($Out))
try {
  $p = Start-Process -FilePath $Exe -PassThru -RedirectStandardOutput "$base.stdout" -RedirectStandardError "$base.stderr"
  $null = $p.Handle
  if ($p.WaitForExit($WaitSeconds * 1000)) { $r = "exit $($p.ExitCode)" }
  else { $r = 'running'; Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
} catch { $r = "error $($_.Exception.Message)" }
Set-Content -LiteralPath $Out -Value $r
'@

Write-Summary "### Launch check: $(Split-Path $Msi -Leaf)"
Write-Summary ""
Write-Summary "| Program | Result | Verdict |"
Write-Summary "|---|---|---|"
$failed = @()
$task = 'craft-launch-check'
foreach ($exe in $exes) {
  $leaf = Split-Path $exe -Leaf
  $out = Join-Path $work ("$([IO.Path]::GetFileNameWithoutExtension($exe)).result")
  $arg = "-NoProfile -ExecutionPolicy Bypass -File `"$wrapper`" -Exe `"$exe`" -Out `"$out`" -WaitSeconds $WaitSeconds"
  Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue
  Register-ScheduledTask -TaskName $task -User 'SYSTEM' -RunLevel Highest -Force `
    -Action (New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arg) | Out-Null
  Start-ScheduledTask -TaskName $task
  $deadline = (Get-Date).AddSeconds($WaitSeconds + 60)
  while (-not (Test-Path $out) -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 2 }
  Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue

  $result = if (Test-Path $out) { (Get-Content $out -TotalCount 1).Trim() } else { 'no result' }
  $isMain = $leaf -ieq $MainExe
  $ok = switch -Regex ($result) {
    '^running$' { $true; break }
    '^exit (-?\d+)$' { $code = [int]$Matches[1]; if ($isMain) { $code -eq 0 } else { -not (Test-IsCrash $code) }; break }
    default { $false }
  }
  $role = if ($isMain) { ' (main app)' } else { '' }
  Write-Summary ("| ``{0}``{1} | {2} | {3} |" -f $leaf, $role, $result, $(if ($ok) { 'pass' } else { '**FAIL**' }))
  if (-not $ok) {
    $failed += $leaf
    $stderr = [IO.Path]::ChangeExtension($out, 'stderr')
    Write-Output "::group::$leaf stderr (last 30 lines)"
    Get-Content $stderr -Tail 30 -ErrorAction SilentlyContinue
    Write-Output "::endgroup::"
  }
}

Start-Process msiexec.exe -ArgumentList @('/x', "`"$Msi`"", '/qn', '/norestart') -Wait | Out-Null

if ($failed) {
  Write-Summary ""
  Write-Summary "These programs fail when started without a desktop, as the winget-pkgs validator starts them: $($failed -join ', '). The validator labels that Validation-Executable-Error and the version never reaches WinGet. Report a graphics start failure in a dialog and keep running instead of panicking or exiting."
  Write-Output "::error::Launch check failed for $($failed -join ', ') (see the job summary)"
  exit 1
}
Write-Summary ""
Write-Summary "All programs survive a launch without a desktop."
