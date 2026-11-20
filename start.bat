@echo off
setlocal
cd /d "%~dp0"

rem Prefer a local virtualenv if present, otherwise use system python
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

rem Desktop launcher: use pythonw from the same interpreter directory so no extra console stays open
set "PYEXE="
for /f "delims=" %%I in ('%PY% -c "import sys;print(sys.executable)"') do set "PYEXE=%%I"
set "PYW=%PYEXE:python.exe=pythonw.exe%"
if not exist "%PYW%" set "PYW="

rem Diagnostic mode: start.bat --check
if /I "%~1"=="--check" (
    echo Python : %PY%
    %PY% --version
    %PY% -c "import PyQt6; from PyQt6.QtCore import QT_VERSION_STR; print('PyQt6  :', QT_VERSION_STR)" 2>nul || echo PyQt6  : MISSING
    %PY% -c "import PySide6; print('PySide6:', PySide6.__version__)" 2>nul || echo PySide6: MISSING
    goto :end
)

%PY% -c "import PyQt6" >nul 2>&1
if errorlevel 1 (
    echo [LongLedger] PyQt6 not found, installing ...
    %PY% -m pip install --upgrade PyQt6
    if errorlevel 1 (
        echo.
        echo [LongLedger] Install failed. Please run manually:
        echo     %PY% -m pip install PyQt6
        echo.
        pause
        exit /b 1
    )
)

rem Compile check first, so pythonw never fails silently
%PY% -m py_compile main_app.py desktop\window.py desktop\models.py desktop\theme.py >nul 2>&1
if errorlevel 1 (
    echo [LongLedger] Source check failed. Run in console to see details:
    echo     python main_app.py
    echo.
    pause
    exit /b 1
)

echo [LongLedger] Starting desktop ledger ...
if defined PYW (
    start "" "%PYW%" "main_app.py"
) else (
    "%PY%" "main_app.py"
)

:end
endlocal
