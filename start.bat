@echo off
setlocal
title Clipper
cd /d "%~dp0"

rem ---- find Python -------------------------------------------------------
set "PY="
py --version >nul 2>&1 && set "PY=py"
if not defined PY (
  python --version >nul 2>&1 && set "PY=python"
)
if not defined PY goto nopython

rem ---- find ffmpeg -------------------------------------------------------
where ffmpeg >nul 2>&1
if errorlevel 1 goto noffmpeg

rem ---- install yt-dlp the first time ------------------------------------
%PY% -c "import yt_dlp" >nul 2>&1
if errorlevel 1 (
  echo Installing yt-dlp ^(first run only^)...
  %PY% -m pip install -U yt-dlp
  if errorlevel 1 goto pipfail
)

echo.
echo Starting Clipper... your browser will open.
echo Keep this window open while you use it. Close it to stop Clipper.
echo.
%PY% webui.py
if errorlevel 1 (
  echo.
  echo Clipper stopped with an error ^(see above^).
  pause
)
goto :eof

:nopython
echo.
echo Python was not found.
echo Install it from https://www.python.org/downloads/ and tick "Add Python to PATH",
echo then double-click start.bat again.
echo.
pause
exit /b 1

:noffmpeg
echo.
echo ffmpeg was not found. Trying to install it with winget...
winget install -e --id Gyan.FFmpeg
echo.
echo If that worked, CLOSE this window and double-click start.bat again.
echo If it did not, install ffmpeg manually and make sure it is on your PATH.
echo.
pause
exit /b 1

:pipfail
echo.
echo Could not install yt-dlp. Check your internet connection, then try again.
echo.
pause
exit /b 1
