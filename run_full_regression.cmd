@echo off
setlocal
call "%~dp0scripts\desktop_run.cmd" regression
exit /b %ERRORLEVEL%
