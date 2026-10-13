@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
where uv >nul 2>nul
if errorlevel 1 (
    echo uv is required. Install: winget install --id astral-sh.uv -e
    pause
    exit /b 1
)
uv run python -X utf8 -m auto_location.single_user --gui %*
set "single_exit=%errorlevel%"
echo.
echo Exit code: %single_exit%. Results: .state\single-user\
pause
exit /b %single_exit%
