# Install WednesdayTTS as a Windows Task Scheduler task.
# Run from an elevated PowerShell (right-click -> Run as Administrator).
#
# ASR task is in parent-repo/services/install-services.ps1 — not managed here.
#
# Uses Task Scheduler with Interactive logon so the service runs as the logged-in
# user and has access to the audio session. NSSM services run as SYSTEM (no audio).

param(
    [string]$RepoDir = "C:\dev\wednesday-tts",
    [string]$User    = "$env:USERDOMAIN\$env:USERNAME"
)

Write-Host "Installing WednesdayTTS task for user: $User" -ForegroundColor Cyan
Write-Host "Repo dir: $RepoDir" -ForegroundColor Cyan

# Remove old NSSM service if present (suppress all output — errors are expected if already gone)
$svcObj = Get-Service -Name "WednesdayTTS" -ErrorAction SilentlyContinue
if ($svcObj) {
    Write-Host "Removing old NSSM service: WednesdayTTS" -ForegroundColor Yellow
    if ($svcObj.Status -eq 'Running') {
        Stop-Service -Name "WednesdayTTS" -Force -ErrorAction SilentlyContinue 2>&1 | Out-Null
    }
    & nssm remove WednesdayTTS confirm 2>&1 | Out-Null
}
if (Get-ScheduledTask -TaskName "WednesdayTTS" -ErrorAction SilentlyContinue) {
    Write-Host "Removing existing scheduled task: WednesdayTTS" -ForegroundColor Yellow
    Unregister-ScheduledTask -TaskName "WednesdayTTS" -Confirm:$false
}

# Task settings: no time limit, restart up to 3 times on failure, one instance at a time
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Days 0) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew

# --- WednesdayTTS (port 5678) ---
Write-Host "`nInstalling WednesdayTTS..." -ForegroundColor Green

$ttsPrincipal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
$ttsAction    = New-ScheduledTaskAction `
    -Execute "$RepoDir\.venv\Scripts\pythonw.exe" `
    -Argument "-m wednesday_tts.server.app" `
    -WorkingDirectory $RepoDir
$ttsTrigger   = New-ScheduledTaskTrigger -AtLogOn -User $User

Register-ScheduledTask `
    -TaskName "WednesdayTTS" `
    -Action $ttsAction `
    -Trigger $ttsTrigger `
    -Settings $settings `
    -Principal $ttsPrincipal `
    -Description "Wednesday TTS service (localhost:5678) — runs wednesday_tts.server.app from $RepoDir" `
    -Force | Out-Null

Write-Host "  WednesdayTTS registered." -ForegroundColor Green
