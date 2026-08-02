@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -m pip install --upgrade pyinstaller || goto :err
python -m PyInstaller --noconsole --onefile --name TrayHider tray_hider.py || goto :err
echo.
echo 打包完成: %cd%\dist\TrayHider.exe
pause
exit /b 0
:err
echo 打包失败，请检查上面的输出
pause
