@echo off
setlocal
pushd "%~dp0"
python scripts\download_golden.py %*
set "corona_tests_exit=%ERRORLEVEL%"
popd
exit /b %corona_tests_exit%
