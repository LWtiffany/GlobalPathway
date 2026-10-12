@echo off
chcp 65001 >nul
REM ============================================================
REM  Agent1 手动触发口子（测试用）
REM  双击运行 = 抓取默认来源 uk_student_visa_money
REM  也可命令行带参数：  触发测试.bat [来源ID] [--force]
REM ============================================================
setlocal
REM This entry point always uses development, even if the parent shell selects production.
set "AGENT1_ENV=development"

cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [错误] 找不到虚拟环境 python：%PY%
    echo 请先执行 Setup：.venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)

set "SOURCE=%1"
if "%SOURCE%"=="" set "SOURCE=uk_student_visa_money"
set "FORCE=%2"

echo.
echo ============ Agent1 手动触发 ============
echo   来源：%SOURCE%
if "%FORCE%"=="--force" echo   模式：忽略租约强制执行
echo ========================================
echo.

"%PY%" agent1.py --env development run %SOURCE% %FORCE%

echo.
echo ------------ 触发完成，退出码 %ERRORLEVEL% ------------
echo 查看门控问题：.venv\Scripts\python.exe agent1.py --env development gate
echo 查看运行记录：.venv\Scripts\python.exe agent1.py --env development runs --limit 5
echo.
pause
