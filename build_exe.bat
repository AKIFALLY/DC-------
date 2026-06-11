@echo off
REM DC 發電機量測系統 一鍵打包
REM 產出: dist\DCGenTester.exe + dist\config.yaml
chcp 65001 >nul
cd /d "%~dp0"

echo [1/3] 確認 PyInstaller...
python -m pip show pyinstaller >/dev/null 2>&1 || python -m pip install pyinstaller

echo [2/3] 打包 (onefile)...
python -m PyInstaller DCGenTester.spec --noconfirm
if errorlevel 1 (
    echo 打包失敗，請看上方訊息。
    pause
    exit /b 1
)

echo [3/3] 複製 config.yaml 到 exe 旁邊...
copy /Y config.yaml dist\config.yaml >nul

echo.
echo 完成！產出在 dist\ ：
echo   dist\DCGenTester.exe   （主程式）
echo   dist\config.yaml       （設定檔，現場可直接編輯 IP/額定等）
pause