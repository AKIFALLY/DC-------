@echo off
REM DC 發電機量測系統 安裝精靈打包
REM 前置：先執行 build_exe.bat 產出 dist\DCGenTester.exe
REM 產出: installer\DCGenTester_Setup_1.0.0.exe
chcp 65001 >nul
cd /d "%~dp0"

if not exist "dist\DCGenTester.exe" (
    echo 找不到 dist\DCGenTester.exe，請先執行 build_exe.bat
    pause
    exit /b 1
)

set ISCC="%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist %ISCC% (
    echo 找不到 Inno Setup，請至 https://jrsoftware.org/isinfo.php 安裝後重跑。
    pause
    exit /b 1
)

echo 打包安裝精靈...
%ISCC% installer.iss
if errorlevel 1 (
    echo 打包失敗，請看上方訊息。
    pause
    exit /b 1
)

echo.
echo 完成！產出在 installer\ ：
echo   installer\DCGenTester_Setup_1.0.0.exe
pause