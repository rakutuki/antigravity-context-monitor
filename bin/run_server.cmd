@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
set "SERVER_PY=%SCRIPT_DIR%server.py"

rem 1. PyManager / pythoncore (prefer newest version)
if exist "%LOCALAPPDATA%\Python" (
    for /f "delims=" %%D in ('dir /b /ad /o-n "%LOCALAPPDATA%\Python\pythoncore-*" 2^>nul') do (
        if exist "%LOCALAPPDATA%\Python\%%D\python.exe" (
            "%LOCALAPPDATA%\Python\%%D\python.exe" -u "%SERVER_PY%" %*
            exit /b %ERRORLEVEL%
        )
    )
)

rem 2. Official Python Installer (prefer newest version)
if exist "%LOCALAPPDATA%\Programs\Python" (
    for /f "delims=" %%D in ('dir /b /ad /o-n "%LOCALAPPDATA%\Programs\Python\Python*" 2^>nul') do (
        if exist "%LOCALAPPDATA%\Programs\Python\%%D\python.exe" (
            "%LOCALAPPDATA%\Programs\Python\%%D\python.exe" -u "%SERVER_PY%" %*
            exit /b %ERRORLEVEL%
        )
    )
)

rem 3. Python Launcher (py.exe)
where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    py -3 -u "%SERVER_PY%" %*
    exit /b %ERRORLEVEL%
)

rem 4. Standard PATH python / python3
where python >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    python -u "%SERVER_PY%" %*
    exit /b %ERRORLEVEL%
)

python3 -u "%SERVER_PY%" %*
