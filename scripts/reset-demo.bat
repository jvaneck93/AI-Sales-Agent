@echo off
rem Wis de lokale database en begin opnieuw met verse demodata.
cd /d "%~dp0\.."
if exist "data\salesagent.db" del "data\salesagent.db"
call start.bat
