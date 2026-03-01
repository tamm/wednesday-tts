# Stop Wednesday TTS and ASR scheduled tasks
# Stop-ScheduledTask alone does NOT kill the running process — we must find and kill it.

Write-Host "Stopping Wednesday services..." -ForegroundColor Cyan

Stop-ScheduledTask -TaskName "WednesdayTTS" -ErrorAction SilentlyContinue
Stop-ScheduledTask -TaskName "WednesdayASR" -ErrorAction SilentlyContinue

# Kill any lingering pythonw.exe processes on the TTS/ASR ports
foreach ($port in @(5678, 5679)) {
    $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($conn) {
        $procId = $conn.OwningProcess
        Write-Host "  Killing process $procId on port $port" -ForegroundColor Yellow
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
    }
}

Write-Host "Services stopped." -ForegroundColor Green
