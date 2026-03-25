$here = $PSScriptRoot
$videoScript = Join-Path $here "_run_video.ps1"
$appScript = Join-Path $here "_run_app.ps1"
Start-Process powershell -ArgumentList "-NoExit", "-File", "`"$videoScript`""
Start-Sleep -Seconds 2
Start-Process powershell -ArgumentList "-NoExit", "-File", "`"$appScript`""
Write-Host "http://localhost:8080"
