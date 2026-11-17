@echo off
setlocal
if not "%~1"=="" goto command_line
call "%~dp0scripts\desktop_run.cmd" golden
exit /b %ERRORLEVEL%

:command_line
pushd "%~dp0"
python scripts\generate_golden.py %*
set "corona_tests_exit=%ERRORLEVEL%"
popd
exit /b %corona_tests_exit%
