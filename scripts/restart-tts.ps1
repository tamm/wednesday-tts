# Restart Wednesday TTS and ASR scheduled tasks
# Use this after modifying tts-service.py or asr-service.py
# Note: Stop-ScheduledTask does NOT kill the running process — we must do that explicitly.

Write-Host "Restarting Wednesday services..." -ForegroundColor Cyan

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

Start-Sleep -Seconds 2
Start-ScheduledTask -TaskName "WednesdayTTS" -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName "WednesdayASR" -ErrorAction SilentlyContinue
Start-Sleep -Seconds 3

Write-Host "Status:" -ForegroundColor Yellow
Write-Host "  WednesdayTTS: $((Get-ScheduledTask -TaskName WednesdayTTS -ErrorAction SilentlyContinue).State)" -ForegroundColor Gray
Write-Host "  WednesdayASR: $((Get-ScheduledTask -TaskName WednesdayASR -ErrorAction SilentlyContinue).State)" -ForegroundColor Gray

# Verify endpoints
Write-Host ""
try {
    $r = Invoke-WebRequest -Uri "http://localhost:5678/health" -TimeoutSec 5 -UseBasicParsing
    Write-Host "  TTS health: OK" -ForegroundColor Green
} catch {
    Write-Host "  TTS health: not responding (may still be loading model)" -ForegroundColor Yellow
}
