@echo off
setlocal
cd /d "%~dp0"

rem ============================================================
rem  长衡 LongLedger · 第一段：Nuitka standalone 编译 + 产物对齐
rem  版本号只改这里与检查清单里的三处（见 打包方案 文档）
rem ============================================================
set "VERSION=1.0.0"

set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

echo [1/3] 清理旧产物 ...
if exist "build" rmdir /s /q "build"
if exist "dist\LongLedger" rmdir /s /q "dist\LongLedger"

echo [2/3] Nuitka 编译（首次会下载 MinGW，约数分钟到十几分钟）...
%PY% -m nuitka --standalone --assume-yes-for-downloads ^
  --enable-plugin=pyqt6 ^
  --windows-console-mode=disable ^
  --windows-icon-from-ico=assets\brand\changheng.ico ^
  --include-data-dir=assets=assets ^
  --include-data-files=data\schema.sql=data\schema.sql ^
  --jobs=4 ^
  --output-dir=build ^
  --output-filename=LongLedger.exe ^
  --product-version=%VERSION% --file-version=%VERSION% ^
  --product-name="长衡" --company-name="LongLedger" ^
  --copyright="LongLedger" ^
  main_app.py
if errorlevel 1 goto :fail

echo [3/3] 对齐到 dist\LongLedger（安装器固定输入目录）...
robocopy "build\main_app.dist" "dist\LongLedger" /MIR /NFL /NDL /NJH /NJS /NP
if errorlevel 8 goto :fail

echo.
echo [OK] 产物目录：dist\LongLedger
echo      下一步：用 Inno Setup 编译 installer\LongLedger.iss
exit /b 0

:fail
echo.
echo [FAIL] 编译失败，请查看上方 Nuitka 输出。
pause
exit /b 1
