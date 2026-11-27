@echo off
rem Start de Sales AI Agent op Windows. Dubbelklik dit bestand.
rem Eerste keer: maakt een Python-omgeving, installeert alles en vult de demodata.
rem "start.bat leeg" begint zonder demodata.
setlocal
cd /d "%~dp0"
chcp 65001 >nul

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo Python is niet gevonden. Installeer Python 3.12 of nieuwer via https://www.python.org/downloads/
    echo Vink bij de installatie "Add python.exe to PATH" aan en start dit bestand opnieuw.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Python-omgeving aanmaken...
    %PY% -m venv .venv || goto :fout
    ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
    echo Onderdelen installeren, dit duurt een paar minuten...
    ".venv\Scripts\python.exe" -m pip install -e . || goto :fout
)

if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo .env aangemaakt vanuit .env.example. DRY_RUN staat aan: er wordt niet echt gebeld.
)

if not exist "data" mkdir data
if not exist "data\import" mkdir "data\import"
set "NIEUW="
if not exist "data\salesagent.db" set "NIEUW=1"
".venv\Scripts\salesagent.exe" db init || goto :fout
if defined NIEUW if /i not "%~1"=="leeg" (
    echo Demodata aanmaken met verzonnen bedrijven...
    ".venv\Scripts\salesagent.exe" demo >nul || goto :fout
)

echo.
echo De webpagina opent zo in je browser. Laat dit venster open; sluiten = stoppen.
".venv\Scripts\salesagent.exe" web --browser
goto :eof

:fout
echo.
echo Er ging iets mis. Stuur de melding hierboven door.
pause
exit /b 1
