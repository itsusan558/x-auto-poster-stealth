$videoDir = Join-Path (Split-Path $PSScriptRoot -Parent) "hf-video-compiler"
Set-Location $videoDir
py web_compiler.py
