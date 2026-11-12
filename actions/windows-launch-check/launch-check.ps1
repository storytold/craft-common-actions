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

  Needs a machine where the product isn't installed yet (a fresh runner): it finds the
  programs to start as the .exe files the install added, and refuses to run otherwise.
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

# Windows Installer's COM objects have no type information, so members go through InvokeMember.
function Invoke-Com($object, [string] $name, [string] $kind, [object[]] $arguments = @()) {
  $flags = [Reflection.BindingFlags]::$kind
  return $object.GetType().InvokeMember($name, $flags, $null, $object, $arguments)
}

# The MSI's ProductCode and UpgradeCode, read from its Property table.
function Get-MsiCodes([string] $path) {
  $installer = New-Object -ComObject WindowsInstaller.Installer
  $db = Invoke-Com $installer 'OpenDatabase' 'InvokeMethod' @($path, 0)
  $codes = @{}
  foreach ($name in 'ProductCode', 'UpgradeCode') {
    $view = Invoke-Com $db 'OpenView' 'InvokeMethod' @("SELECT ``Value`` FROM ``Property`` WHERE ``Property`` = '$name'")
    Invoke-Com $view 'Execute' 'InvokeMethod' | Out-Null
    $record = Invoke-Com $view 'Fetch' 'InvokeMethod'
    if ($record) { $codes[$name] = Invoke-Com $record 'StringData' 'GetProperty' @(1) }
    Invoke-Com $view 'Close' 'InvokeMethod' | Out-Null
  }
  return @{ Installer = $installer; Codes = $codes }
}

# A path or a wildcard that matches exactly one MSI (release.yml passes dist/release/*-windows-x64.msi).
$found = @(Resolve-Path -Path $Msi -ErrorAction SilentlyContinue | ForEach-Object Path)
if ($found.Count -ne 1) { Write-Output "::error::'$Msi' must match exactly one MSI; it matches $($found.Count)"; exit 1 }
$Msi = $found[0]
$work = Join-Path ([IO.Path]::GetTempPath()) ("launch-check-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force $work | Out-Null

# The programs to start are the .exe files the install adds, so an earlier install of this
# product (or another version of it) would hide them: refuse rather than test the wrong thing.
$msiInfo = Get-MsiCodes $Msi
$productCode = $msiInfo.Codes['ProductCode']
$upgradeCode = $msiInfo.Codes['UpgradeCode']
$present = @()
if ($productCode -and (Invoke-Com $msiInfo.Installer 'ProductState' 'GetProperty' @($productCode)) -eq 5) { $present += $productCode }
if ($upgradeCode) { $present += @(Invoke-Com $msiInfo.Installer 'RelatedProducts' 'GetProperty' @($upgradeCode)) }
if ($present) {
  Write-Output "::error::A version of this product is already installed ($($present -join ', ')). The launch check needs a machine without it, such as a fresh runner."
  exit 1
}

# Install the way the current manifests do: msiexec /qn with no extra properties.
$before = @(Get-InstalledExes)
$msiLog = Join-Path $work 'install.log'
$install = Start-Process msiexec.exe -ArgumentList @('/i', "`"$Msi`"", '/qn', '/norestart', '/l*v', "`"$msiLog`"") -Wait -PassThru
if ($install.ExitCode -ne 0 -and $install.ExitCode -ne 3010) {
  Get-Content $msiLog -Tail 40 -ErrorAction SilentlyContinue
  Write-Output "::error::msiexec failed with exit code $($install.ExitCode) installing $(Split-Path $Msi -Leaf)"
  exit 1
}

# The scheduled task runs this wrapper, which starts the exe and records how it ended. It writes
# the result to a temporary file and renames it, so the result file only ever appears complete.
# On a timeout it stops the program and everything it started.
$wrapper = Join-Path $work 'run-one.ps1'
Set-Content -LiteralPath $wrapper -Encoding utf8 -Value @'
param([string] $Exe, [string] $Base, [int] $WaitSeconds)
try {
  $p = Start-Process -FilePath $Exe -PassThru -RedirectStandardOutput "$Base.stdout" -RedirectStandardError "$Base.stderr"
  $null = $p.Handle
  if ($p.WaitForExit($WaitSeconds * 1000)) { $r = "exit $($p.ExitCode)" }
  else { $r = 'running'; & taskkill.exe /PID $p.Id /T /F | Out-Null }
} catch { $r = "error $($_.Exception.Message)" }
Set-Content -LiteralPath "$Base.result.tmp" -Value $r
Move-Item -LiteralPath "$Base.result.tmp" -Destination "$Base.result" -Force
'@

$failed = @()
$task = 'craft-launch-check'
try {
  $exes = @(Get-InstalledExes | Where-Object { $before -notcontains $_ })
  if (-not $exes) { Write-Output "::error::Installing $(Split-Path $Msi -Leaf) added no .exe"; exit 1 }
  $mains = @($exes | Where-Object { (Split-Path $_ -Leaf) -ieq $MainExe })
  if (-not $mains) {
    Write-Output "::error::$MainExe is not among the installed programs: $(($exes | ForEach-Object { Split-Path $_ -Leaf }) -join ', ')"
    exit 1
  }
  # The main app is held to exit 0, the other programs only to not crash, so a helper that shares
  # the main app's file name would be judged as the main app. Refuse to guess which one it is.
  if ($mains.Count -gt 1) {
    Write-Output "::error::More than one installed program is named ${MainExe}: $($mains -join ', '). Set main-exe to a name only the main app has."
    exit 1
  }
  $mainPath = $mains[0]

  Write-Summary "### Launch check: $(Split-Path $Msi -Leaf)"
  Write-Summary ""
  Write-Summary "| Program | Result | Verdict |"
  Write-Summary "|---|---|---|"
  $index = 0
  foreach ($exe in $exes) {
    $index++
    $leaf = Split-Path $exe -Leaf
    # Numbered, so two programs with the same file name in different folders never share files.
    $base = Join-Path $work ("{0:d3}-{1}" -f $index, [IO.Path]::GetFileNameWithoutExtension($exe))
    $arg = "-NoProfile -ExecutionPolicy Bypass -File `"$wrapper`" -Exe `"$exe`" -Base `"$base`" -WaitSeconds $WaitSeconds"
    Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName $task -User 'SYSTEM' -RunLevel Highest -Force `
      -Action (New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arg) | Out-Null
    Start-ScheduledTask -TaskName $task
    $deadline = (Get-Date).AddSeconds($WaitSeconds + 60)
    while (-not (Test-Path "$base.result") -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 2 }
    if (-not (Test-Path "$base.result")) {
      # The wrapper itself stalled: stop the task and anything still running from the install.
      Stop-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
      Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $exes -contains $_.Path } |
        ForEach-Object { & taskkill.exe /PID $_.Id /T /F | Out-Null }
    }
    Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue

    $result = if (Test-Path "$base.result") { (Get-Content "$base.result" -TotalCount 1).Trim() } else { 'no result' }
    $isMain = $exe -eq $mainPath
    $ok = switch -Regex ($result) {
      '^running$' { $true; break }
      '^exit (-?\d+)$' { $code = [int]$Matches[1]; if ($isMain) { $code -eq 0 } else { -not (Test-IsCrash $code) }; break }
      default { $false }
    }
    $role = if ($isMain) { ' (main app)' } else { '' }
    Write-Summary ("| ``{0}``{1} | {2} | {3} |" -f $leaf, $role, $result, $(if ($ok) { 'pass' } else { '**FAIL**' }))
    if (-not $ok) {
      $failed += $leaf
      Write-Output "::group::$leaf stderr (last 30 lines)"
      Get-Content "$base.stderr" -Tail 30 -ErrorAction SilentlyContinue
      Write-Output "::endgroup::"
    }
  }
} finally {
  Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue
  $uninstall = Start-Process msiexec.exe -ArgumentList @('/x', "`"$Msi`"", '/qn', '/norestart') -Wait -PassThru
  if ($uninstall.ExitCode -ne 0 -and $uninstall.ExitCode -ne 3010) {
    Write-Output "::warning::Uninstalling $(Split-Path $Msi -Leaf) after the check exited with $($uninstall.ExitCode)"
  }
}

if ($failed) {
  Write-Summary ""
  Write-Summary "These programs fail when started without a desktop, as the winget-pkgs validator starts them: $($failed -join ', '). The validator labels that Validation-Executable-Error and the version never reaches WinGet. Report a graphics start failure in a dialog and keep running instead of panicking or exiting."
  Write-Output "::error::Launch check failed for $($failed -join ', ') (see the job summary)"
  exit 1
}
Write-Summary ""
Write-Summary "All programs survive a launch without a desktop."
