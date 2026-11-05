# AI 작업 지도 — Windows 자동 실행 등록 (관리자 권한 필요 없음)
# Windows 기록과 실행 중인 WSL 배포판의 기록을 함께 모읍니다.
param([switch]$Uninstall)
$ErrorActionPreference = 'Stop'
$dir     = Split-Path -Parent $PSScriptRoot
$startup = [Environment]::GetFolderPath('Startup')
$desktop = [Environment]::GetFolderPath('Desktop')
$lnk     = Join-Path $startup 'AI 작업 지도.lnk'
$url     = Join-Path $desktop 'AI 작업 지도.url'

function Stop-Agent {
  Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
    Where-Object { $_.CommandLine -like '*aiworkmap.py*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

if ($Uninstall) {
  Stop-Agent
  Remove-Item $lnk, $url -ErrorAction SilentlyContinue
  Write-Host '✅ 자동 실행을 해제했습니다. (모아 둔 요약은 %USERPROFILE%\.ai-work-map 에 남습니다)'
  exit 0
}

# 진짜 Python 찾기 (Microsoft Store 가짜 바로가기는 제외)
$py = $null
$cands = @(
  @{ Exe = 'py';     Args = @('-3') },
  @{ Exe = 'python'; Args = @() }
)
foreach ($c in $cands) {
  try {
    $a = $c.Args
    $exe = & $c.Exe @a -c 'import sys; print(sys.executable)' 2>$null
    if ($LASTEXITCODE -eq 0 -and $exe -and ("$exe" -notlike '*WindowsApps*')) { $py = "$exe".Trim(); break }
  } catch {}
}
if (-not $py) {
  Write-Host '❌ Python이 없습니다. 먼저 설치한 뒤 다시 실행하세요:'
  Write-Host '   winget install Python.Python.3.12'
  exit 1
}
$pyw = Join-Path (Split-Path $py) 'pythonw.exe'
if (-not (Test-Path $pyw)) { $pyw = $py }

Stop-Agent
$sh = New-Object -ComObject WScript.Shell
$s = $sh.CreateShortcut($lnk)
$s.TargetPath       = $pyw
$s.Arguments        = "`"$dir\aiworkmap.py`" run --no-open"
$s.WorkingDirectory = $dir
$s.WindowStyle      = 7
$s.Description      = 'AI 작업 지도 수집기·뷰어'
$s.Save()
Set-Content -Path $url -Value "[InternetShortcut]`r`nURL=http://127.0.0.1:8765/" -Encoding ASCII

Start-Process -FilePath $pyw -ArgumentList "`"$dir\aiworkmap.py`" run --no-open" -WorkingDirectory $dir -WindowStyle Hidden
Start-Sleep -Seconds 3
Write-Host '✅ 설치했습니다. 로그인할 때마다 자동으로 실행됩니다.'
Write-Host '   화면: http://127.0.0.1:8765/  (바탕화면 ''AI 작업 지도'' 바로가기)'
Write-Host '   WSL 기록은 WSL이 켜져 있는 동안 함께 모읍니다.'
Start-Process 'http://127.0.0.1:8765/'
