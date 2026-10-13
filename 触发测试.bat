@echo off
chcp 65001 >nul
REM ============================================================
REM  Agent1 手动触发口子（测试用）
REM  双击运行 = 增量抓取 UK / US / CN-HK 全部签证来源（scan-region all）
REM  也可命令行带参数：  触发测试.bat [区域 UK/US/CN-HK/all 或 来源ID] [--force]
REM  [--force] = 强制全量重抽（--full）
REM ============================================================
setlocal
REM This entry point always uses development, even if the parent shell selects production.
set "AGENT1_ENV=development"

REM LLM 接入（阿里云 MaaS 工作区）。可被外部环境变量覆盖。
if not defined AGENT1_LLM_PROVIDER set "AGENT1_LLM_PROVIDER=openai/deepseek-v4.1-flash"
if not defined AGENT1_LLM_BASE_URL set "AGENT1_LLM_BASE_URL=https://llm-tlndh47wd9ustbss.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"

REM Crawl4AI 的缓存/robots 目录必须落在项目内（沙箱下用户主目录无法使用 SQLite WAL）
if not defined CRAWL4_AI_BASE_DIRECTORY set "CRAWL4_AI_BASE_DIRECTORY=%~dp0data\_crawl4ai"

cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [错误] 找不到虚拟环境 python：%PY%
    echo 请先执行 Setup：.venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)

set "SOURCE=%1"
if "%SOURCE%"=="" set "SOURCE=all"
set "FORCE=%2"
set "FORCEOPT="
if "%FORCE%"=="--force" set "FORCEOPT=--full"
set "SCANCMD=scan"
for %%R in (all UK US CN-HK) do if /I "%SOURCE%"=="%%R" set "SCANCMD=scan-region"

echo.
echo ============ Agent1 手动触发 ============
echo   目标：%SOURCE%（%SCANCMD%）
if "%FORCE%"=="--force" echo   模式：强制全量重抽
echo ========================================
echo.

"%PY%" agent1.py --env development %SCANCMD% %SOURCE% %FORCEOPT%

echo.
echo ------------ 触发完成，退出码 %ERRORLEVEL% ------------
echo 查看待审核项：.venv\Scripts\python.exe agent1.py --env development review list
echo 查看库状态：.venv\Scripts\python.exe agent1.py --env development db status
echo.
pause
