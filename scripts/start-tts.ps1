# Start Wednesday TTS and ASR scheduled tasks

Write-Host "Starting Wednesday services..." -ForegroundColor Cyan

Start-ScheduledTask -TaskName "WednesdayTTS" -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName "WednesdayASR" -ErrorAction SilentlyContinue

Start-Sleep -Seconds 2

Write-Host "Status:" -ForegroundColor Yellow
Write-Host "  WednesdayTTS: $((Get-ScheduledTask -TaskName WednesdayTTS -ErrorAction SilentlyContinue).State)" -ForegroundColor Gray
Write-Host "  WednesdayASR: $((Get-ScheduledTask -TaskName WednesdayASR -ErrorAction SilentlyContinue).State)" -ForegroundColor Gray
