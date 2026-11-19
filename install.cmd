@echo off
rem Starts install.ps1 without any PowerShell settings, and keeps the window open.
rem Everything is on ONE line: cmd reads a batch file line by line while it runs, and an
rem update replaces this file. The exit code is the one of install.ps1.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %* & call set "CODE=%%ERRORLEVEL%%" & echo.& pause & call exit /b %%CODE%%
