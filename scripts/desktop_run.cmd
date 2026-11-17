@echo off
setlocal
title CoronaTests - %~1
pushd "%~dp0.."

set "corona_desktop_kind=%~1"
set "corona_desktop_config=%CD%\config.%corona_desktop_kind%.local.json"
set "corona_desktop_script=run_regression.py"
if "%corona_desktop_kind%"=="golden" set "corona_desktop_script=generate_golden.py"
set "corona_desktop_stamp="
for /f "delims=" %%I in ('python -c "from datetime import datetime; print(datetime.now().strftime('%%Y%%m%%dT%%H%%M%%S%%f'))"') do set "corona_desktop_stamp=%%I"
if not defined corona_desktop_stamp goto python_missing

set "corona_desktop_id=%corona_desktop_kind%-%corona_desktop_stamp%"
set "corona_desktop_output=%CD%\.workspace\experiments\%corona_desktop_kind%\%corona_desktop_id%"
echo Running all 35 scenes with all 3 algorithms.
echo Config: %corona_desktop_config%
echo Output: %corona_desktop_output%
echo.
python "scripts\%corona_desktop_script%" --config "%corona_desktop_config%" --suite full --baseline-id "%corona_desktop_id%" --output "%corona_desktop_output%"
set "corona_desktop_exit=%ERRORLEVEL%"

echo.
if "%corona_desktop_exit%"=="0" (echo %corona_desktop_kind% PASSED.) else (echo %corona_desktop_kind% FAILED. Exit code: %corona_desktop_exit%)
if exist "%corona_desktop_output%\index.html" (
    echo Report: %corona_desktop_output%\index.html
    start "" "%corona_desktop_output%\index.html"
)
echo.
pause
popd
exit /b %corona_desktop_exit%

:python_missing
echo Python could not start. Install Python and the dependencies in requirements.txt.
pause
popd
exit /b 1
