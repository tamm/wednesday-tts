# Check status of Wednesday TTS and ASR scheduled tasks

Write-Host "Wednesday Service Status:" -ForegroundColor Cyan
Write-Host ""

$tts = Get-ScheduledTask -TaskName "WednesdayTTS" -ErrorAction SilentlyContinue
$asr = Get-ScheduledTask -TaskName "WednesdayASR" -ErrorAction SilentlyContinue

if ($tts) {
    Write-Host "WednesdayTTS (port 5678):  $($tts.State)" -ForegroundColor Yellow
} else {
    Write-Host "WednesdayTTS: not registered" -ForegroundColor Red
}

if ($asr) {
    Write-Host "WednesdayASR (port 5679):  $($asr.State)" -ForegroundColor Yellow
} else {
    Write-Host "WednesdayASR: not registered" -ForegroundColor Red
}

Write-Host ""
Write-Host "Testing endpoints..." -ForegroundColor Cyan

try {
    $r = Invoke-WebRequest -Uri "http://localhost:5678/health" -TimeoutSec 3 -UseBasicParsing
    Write-Host "  TTS: OK  ($($r.Content))" -ForegroundColor Green
} catch {
    Write-Host "  TTS: not responding" -ForegroundColor Red
}

try {
    $r = Invoke-WebRequest -Uri "http://localhost:5679/health" -TimeoutSec 3 -UseBasicParsing
    Write-Host "  ASR: OK  ($($r.Content))" -ForegroundColor Green
} catch {
    Write-Host "  ASR: not responding" -ForegroundColor Red
}
