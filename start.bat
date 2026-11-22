@echo off
chcp 65001 >nul
cd /d "%~dp0"
title FL.Radar - мониторинг заказов
echo ============================================
echo   FL.Radar - запуск мониторинга заказов
echo ============================================
echo.

where python >nul 2>nul
if %errorlevel%==0 (
    python run.py
) else (
    py run.py
)

echo.
echo Сервер остановлен. Нажмите любую клавишу для выхода.
pause >nul