$ErrorActionPreference = 'Continue'
$log = 'C:\Users\mi\.dsh\restart-status.log'
function Log($m){ Add-Content -Path $log -Value ("$(Get-Date -Format o) " + $m) -Encoding utf8 }
$cwd = $env:DSH_RESTART_CWD; if (-not $cwd) { $cwd = $HOME }

# ── 检测改动类型: settings.yaml 热加载即可, 只有 cordis.patch.yml 才需重启 ──
$settingsFile = "$HOME\.dsh\settings.yaml"
$patchFile = "$HOME\.dsh\profiles\web\cordis.patch.yml"
$markerFile = "$HOME\.dsh\.last-restart"

# 上次重启时间(或文件最后写入时间)
$lastRestart = if (Test-Path $markerFile) { (Get-Item $markerFile).LastWriteTime } else { [datetime]::MinValue }

$settingsChanged = (Get-Item $settingsFile -EA SilentlyContinue).LastWriteTime -gt $lastRestart
$patchChanged = if (Test-Path $patchFile) { (Get-Item $patchFile).LastWriteTime -gt $lastRestart } else { $false }

if ($patchChanged) {
    Log "=== restart required: cordis.patch.yml changed ==="
} elseif ($settingsChanged) {
    Log "=== settings.yaml changed — DSH watcher hot-reloads, skipping restart ==="
    Write-Host "✅ 配置已热加载 (settings.yaml), 无需重启 DSH web"
    # 更新 marker
    Set-Content -Path $markerFile -Value (Get-Date -Format o) -Encoding utf8
    exit 0
} else {
    Log "=== restart requested (no file change detected) — proceeding ==="
}

# ── 重启流程 ──
Log "=== restart scheduled; grace 10s; cwd=$cwd ==="
Start-Sleep -Seconds 10
$conn = Get-NetTCPConnection -LocalPort 3080 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($conn) {
  $webPid = $conn.OwningProcess
  Log "stopping web server pid=$webPid"
  Stop-Process -Id $webPid -Force -ErrorAction SilentlyContinue
  try {
    $ppid = (Get-CimInstance Win32_Process -Filter "ProcessId=$webPid" -ErrorAction SilentlyContinue).ParentProcessId
    if ($ppid) { Log "stopping parent npx pid=$ppid"; Stop-Process -Id $ppid -Force -ErrorAction SilentlyContinue }
  } catch {}
} else { Log 'no :3080 listener found at stop time' }
Start-Sleep -Seconds 3
$t = 0
while ($t -lt 15 -and (Get-NetTCPConnection -LocalPort 3080 -State Listen -ErrorAction SilentlyContinue)) { Start-Sleep 1; $t++ }
Log "port free after ${t}s; starting new dsh web"
if ($cwd) { Set-Location $cwd }
$env:DSH_PERMISSION_MODE = 'danger-full-access'
$webOut = 'C:\Users\mi\AppData\Local\Temp\dsh-capture\dsh-web.out'
$webErr = 'C:\Users\mi\AppData\Local\Temp\dsh-capture\dsh-web.err'
$p = Start-Process -FilePath 'cmd.exe' -ArgumentList "/c dsh web > $webOut 2> $webErr" -WorkingDirectory $cwd -WindowStyle Hidden -PassThru
Log "launched npx pid=$($p.Id)"
Start-Sleep -Seconds 30
$c2 = Get-NetTCPConnection -LocalPort 3080 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($c2) {
    Log "OK: :3080 listening pid=$($c2.OwningProcess)"
    # 更新 marker 文件(记录本次重启时间)
    Set-Content -Path $markerFile -Value (Get-Date -Format o) -Encoding utf8
} else { Log 'WARN: :3080 not listening after 30s (still booting/rebuilding?)' }
