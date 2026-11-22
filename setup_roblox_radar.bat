@echo off
setlocal enabledelayedexpansion
title Roblox Radar - GlobalStats Dependency Installer ^& Launcher
color 0B
cd /d "%~dp0"

echo ===============================================================================
echo   [+] ROBLOX RADAR - GLOBALSTATS ENVIRONMENT ^& DEPENDENCY INSTALLER
echo   Working Directory: %CD%
echo ===============================================================================
echo.

:: ------------------------------------------------------------------------------
:: 1. DEEP MULTI-LOCATION PYTHON SEARCH
:: ------------------------------------------------------------------------------
echo [*] Searching for a working Python interpreter across the system...

set "PY_CMD="

:: A. Check standard commands on PATH
call :TEST_PYTHON "python"
if defined PY_CMD goto :PYTHON_FOUND

call :TEST_PYTHON "py -3"
if defined PY_CMD goto :PYTHON_FOUND

call :TEST_PYTHON "py"
if defined PY_CMD goto :PYTHON_FOUND

:: B. Check User LocalAppData Programs directory (Python 3.8 to 3.16)
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
    if exist "%%D\python.exe" (
        call :TEST_PYTHON "%%D\python.exe"
        if defined PY_CMD goto :PYTHON_FOUND
    )
)

:: C. Check 64-bit Program Files directory
for /d %%D in ("%ProgramFiles%\Python3*") do (
    if exist "%%D\python.exe" (
        call :TEST_PYTHON "%%D\python.exe"
        if defined PY_CMD goto :PYTHON_FOUND
    )
)

:: D. Check 32-bit Program Files (x86) directory
if defined ProgramFiles(x86) (
    for /d %%D in ("%ProgramFiles(x86)%\Python3*") do (
        if exist "%%D\python.exe" (
            call :TEST_PYTHON "%%D\python.exe"
            if defined PY_CMD goto :PYTHON_FOUND
        )
    )
)

:: E. Check Root C:\ directory installs
for /d %%D in ("C:\Python3*") do (
    if exist "%%D\python.exe" (
        call :TEST_PYTHON "%%D\python.exe"
        if defined PY_CMD goto :PYTHON_FOUND
    )
)

:: F. Check Windows Registry (HKCU & HKLM PythonCore InstallPath)
for /f "tokens=2*" %%A in ('reg query "HKCU\Software\Python\PythonCore" /s /v "InstallPath" 2^>nul ^| findstr /i "REG_SZ REG_EXPAND_SZ"') do (
    set "REG_PY_DIR=%%B"
    if exist "!REG_PY_DIR!\python.exe" (
        call :TEST_PYTHON "!REG_PY_DIR!\python.exe"
        if defined PY_CMD goto :PYTHON_FOUND
    )
    if exist "!REG_PY_DIR!python.exe" (
        call :TEST_PYTHON "!REG_PY_DIR!python.exe"
        if defined PY_CMD goto :PYTHON_FOUND
    )
)

for /f "tokens=2*" %%A in ('reg query "HKLM\Software\Python\PythonCore" /s /v "InstallPath" 2^>nul ^| findstr /i "REG_SZ REG_EXPAND_SZ"') do (
    set "REG_PY_DIR=%%B"
    if exist "!REG_PY_DIR!\python.exe" (
        call :TEST_PYTHON "!REG_PY_DIR!\python.exe"
        if defined PY_CMD goto :PYTHON_FOUND
    )
    if exist "!REG_PY_DIR!python.exe" (
        call :TEST_PYTHON "!REG_PY_DIR!python.exe"
        if defined PY_CMD goto :PYTHON_FOUND
    )
)

:: G. Check Anaconda / Miniconda paths
set "CONDA_PATHS=%USERPROFILE%\anaconda3;%USERPROFILE%\miniconda3;%LOCALAPPDATA%\anaconda3;%LOCALAPPDATA%\miniconda3;C:\ProgramData\anaconda3;C:\ProgramData\miniconda3"
for %%P in (%CONDA_PATHS%) do (
    if exist "%%P\python.exe" (
        call :TEST_PYTHON "%%P\python.exe"
        if defined PY_CMD goto :PYTHON_FOUND
    )
)

:: ------------------------------------------------------------------------------
:: 2. AUTO-INSTALL PYTHON IF NOT FOUND
:: ------------------------------------------------------------------------------
color 0E
echo.
echo [-] No working Python 3.8+ interpreter detected on this system.
echo [*] Starting automated Python installation...
echo.

where winget >nul 2>&1
if not errorlevel 1 (
    echo [*] Installing Python 3.11 via Windows Package Manager (winget)...
    winget install -e --id Python.Python.3.11 --accept-package-agreements --accept-source-agreements
    goto :REFRESH_ENV
)

echo [*] Downloading Python 3.11 installer via PowerShell...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p = Join-Path $env:TEMP 'py_installer.exe'; Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe' -OutFile $p; Start-Process $p -ArgumentList '/quiet InstallAllUsers=1 PrependPath=1 Include_pip=1' -Wait; Remove-Item $p -Force;"

:REFRESH_ENV
for /f "tokens=2*" %%A in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v Path 2^>nul') do set "PATH=%%B;%PATH%"
for /f "tokens=2*" %%A in ('reg query "HKCU\Environment" /v Path 2^>nul') do set "PATH=%%B;%PATH%"

call :TEST_PYTHON "python"
if defined PY_CMD goto :PYTHON_FOUND

call :TEST_PYTHON "py -3"
if defined PY_CMD goto :PYTHON_FOUND

for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
    if exist "%%D\python.exe" (
        call :TEST_PYTHON "%%D\python.exe"
        if defined PY_CMD goto :PYTHON_FOUND
    )
)

color 0C
echo.
echo ===============================================================================
echo [!] Automatic Python setup could not complete.
echo Please install Python manually from: https://www.python.org/downloads/
echo (Check the box that says 'Add python.exe to PATH' during installation).
echo ===============================================================================
echo.
pause
exit /b 1

:: ------------------------------------------------------------------------------
:: 3. PYTHON VERIFIED - PROCEED TO PACKAGES
:: ------------------------------------------------------------------------------
:PYTHON_FOUND
color 0A
echo [+] Active Python interpreter found:
echo     %PY_CMD%
%PY_CMD% --version
echo.

:: Safe spec discovery test for all required packages
%PY_CMD% -c "import importlib.util as u, sys; pkgs=['requests','webview','winotify','psutil','colorama']; sys.exit(0 if all(u.find_spec(p) is not None for p in pkgs) else 1)" >nul 2>&1
if not errorlevel 1 (
    echo ===============================================================================
    echo  [+] All required packages are already installed!
    echo ===============================================================================
    goto :RUN_POST_AUDIT
)

echo [*] One or more dependencies missing. Upgrading pip...
%PY_CMD% -m pip install --upgrade pip --quiet

echo [*] Installing required packages (requests, pywebview, winotify, psutil, colorama)...
if exist "requirements.txt" (
    %PY_CMD% -m pip install -r requirements.txt
) else (
    %PY_CMD% -m pip install requests pywebview winotify psutil colorama
)

if errorlevel 1 (
    color 0C
    echo.
    echo [-] Pip encountered an error during installation.
    echo Attempting user-level installation (--user)...
    %PY_CMD% -m pip install --user requests pywebview winotify psutil colorama
    if errorlevel 1 (
        echo [!] Failed to install dependencies. Please check network connectivity.
        pause
        exit /b 1
    )
)

:: ------------------------------------------------------------------------------
:: 4. INDIVIDUAL DEPENDENCY INTEGRITY CHECK
:: ------------------------------------------------------------------------------
:RUN_POST_AUDIT
echo.
echo [*] Running post-install dependency integrity check...

set "MISSING_DEP=0"

call :CHECK_PACKAGE "requests" "requests"
call :CHECK_PACKAGE "pywebview" "webview"
call :CHECK_PACKAGE "winotify" "winotify"
call :CHECK_PACKAGE "psutil" "psutil"
call :CHECK_PACKAGE "colorama" "colorama"

if "%MISSING_DEP%"=="1" (
    color 0C
    echo.
    echo ===============================================================================
    echo [!] CRITICAL ERROR: One or more dependencies failed verification.
    echo Please run: %PY_CMD% -m pip install --user ^<package_name^>
    echo ===============================================================================
    echo.
    pause
    exit /b 1
)

echo.
echo ===============================================================================
echo  [+] All dependencies verified successfully!
echo ===============================================================================

:: ------------------------------------------------------------------------------
:: 5. LOCATE & LAUNCH ROBLOX RADAR
:: ------------------------------------------------------------------------------
echo.
echo [*] Locating roblox_radar.py...

set "TARGET_PY="
if exist "%~dp0roblox_radar.py" set "TARGET_PY=%~dp0roblox_radar.py"
if not defined TARGET_PY if exist "roblox_radar.py" set "TARGET_PY=roblox_radar.py"
if not defined TARGET_PY if exist "%CD%\roblox_radar.py" set "TARGET_PY=%CD%\roblox_radar.py"
if not defined TARGET_PY if exist "%~dp0..\roblox_radar.py" set "TARGET_PY=%~dp0..\roblox_radar.py"
if not defined TARGET_PY if exist "%USERPROFILE%\Desktop\roblox_radar.py" set "TARGET_PY=%USERPROFILE%\Desktop\roblox_radar.py"
if not defined TARGET_PY if exist "%USERPROFILE%\OneDrive\Desktop\roblox_radar.py" set "TARGET_PY=%USERPROFILE%\OneDrive\Desktop\roblox_radar.py"

if not defined TARGET_PY (
    for /f "delims=" %%F in ('dir /b /s "%~dp0roblox_radar.py" 2^>nul') do (
        if exist "%%F" set "TARGET_PY=%%F"
    )
)

if not defined TARGET_PY (
    color 0E
    echo.
    echo [-] 'roblox_radar.py' was not found in:
    echo     %CD%
    echo     %~dp0
    echo.
    echo Python files currently in this directory:
    dir /b *.py 2>nul
    echo.
    echo [*] Ensure 'roblox_radar.py' is placed in this folder and try again.
    echo.
    pause
    exit /b 0
)

echo [+] Found script at: %TARGET_PY%
echo [*] Launching application...
echo ===============================================================================
echo.

%PY_CMD% "%TARGET_PY%"
set "APP_EXIT_CODE=%ERRORLEVEL%"

if not "%APP_EXIT_CODE%"=="0" (
    color 0C
    echo.
    echo ===============================================================================
    echo [!] roblox_radar.py exited with error code: %APP_EXIT_CODE%
    echo ===============================================================================
    echo.
    pause
    exit /b %APP_EXIT_CODE%
) else (
    echo.
    echo [*] Roblox Radar session ended cleanly.
    pause
)
exit /b 0

:: ------------------------------------------------------------------------------
:: SUBROUTINES
:: ------------------------------------------------------------------------------

:: Tests if a given Python executable exists, runs, and is Python 3.8+
:TEST_PYTHON
if defined PY_CMD exit /b 0
set "CANDIDATE=%~1"
%CANDIDATE% -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>&1
if not errorlevel 1 (
    set "PY_CMD=%CANDIDATE%"
)
exit /b 0

:: Package check via importlib.util.find_spec (safe brackets, no parens in if block)
:CHECK_PACKAGE
%PY_CMD% -c "import importlib.util as u, sys; sys.exit(0 if u.find_spec('%~2') is not None else 1)" >nul 2>&1
if errorlevel 1 (
    echo   [-] FAILED: '%~1' [%~2] is unavailable.
    set "MISSING_DEP=1"
) else (
    echo   [+] OK: %~1
)
exit /b 0
