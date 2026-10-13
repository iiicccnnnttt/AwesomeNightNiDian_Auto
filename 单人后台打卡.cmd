@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
where uv >nul 2>nul
if errorlevel 1 (
    echo uv is required. Complete foreground initialization first.
    exit /b 1
)
if not exist ".state\single-user" mkdir ".state\single-user"
uv run --frozen python -X utf8 -m auto_location.single_user --headless %* >> ".state\single-user\background.log" 2>&1
set "single_exit=%errorlevel%"
exit /b %single_exit%
