@echo off
chcp 65001 >nul
cd /d "%~dp0"

powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:8765/' -TimeoutSec 2; if ($r.StatusCode -eq 200) { exit 0 } else { exit 1 } } catch { exit 1 }"
if not errorlevel 1 (
  start "" "http://127.0.0.1:8765/"
  exit /b 0
)

echo Starting CoinGecko Archive web UI...
echo.
python web_app.py --output-dir "%~dp0.."
if errorlevel 1 (
  echo.
  echo Failed to start. Check Python and requirements.txt.
  pause
)
