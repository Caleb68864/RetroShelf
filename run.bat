@echo off
REM RetroShelf launcher (Windows). Sets up a venv, installs deps, runs the server.
REM
REM Usage:
REM   run.bat                                       uses %KAVITA_OPDS_URL% or defaults to ManyBooks
REM   run.bat "http://kavita:5000/api/opds/KEY"     point at your Kavita OPDS URL
setlocal
cd /d "%~dp0"

REM First argument overrides the OPDS URL.
if not "%~1"=="" set "KAVITA_OPDS_URL=%~1"

REM Default to the public ManyBooks OPDS feed so the app runs with zero config.
if "%KAVITA_OPDS_URL%"=="" (
  set "KAVITA_OPDS_URL=https://manybooks.net/opds"
  echo No KAVITA_OPDS_URL set - defaulting to the public ManyBooks feed for testing.
  echo Point at your own library with:  run.bat "http://kavita:5000/api/opds/YOUR_KEY"
)
if "%APP_PORT%"=="" set "APP_PORT=8099"

REM Create the virtualenv on first run.
if not exist ".venv\Scripts\python.exe" (
  echo Creating virtualenv ^(.venv^)...
  python -m venv .venv
)
echo Installing dependencies...
.venv\Scripts\python -m pip install -q --upgrade pip
.venv\Scripts\python -m pip install -q -r requirements.txt

echo.
echo RetroShelf starting on  http://0.0.0.0:%APP_PORT%
echo Open it from an iPad at http://^<this-computer-ip^>:%APP_PORT%
echo.
.venv\Scripts\python -m uvicorn app.main:app --host 0.0.0.0 --port %APP_PORT%
endlocal
