@echo off
rem Shared launcher: %1 = exe name in game\binaries\win32, %2..%4 = extra client args. Starts the server with its output
rem tee'd to poc-server\logs\server-<stamp>.log, archives the previous client log, then starts
rem the client. Logs to send when something goes wrong are all in poc-server\logs\.
setlocal
set "EXE=%~1"
set "HERE=%~dp0.."
set "GAME=%HERE%\..\game"
set "LOGS=%HERE%\logs"
set "DOCS=%USERPROFILE%\Documents\survarium"
if not exist "%GAME%\binaries\win32\%EXE%" (
  echo %EXE% not found under "%GAME%\binaries\win32" & pause & exit /b 1
)
if not exist "%LOGS%" mkdir "%LOGS%"
rem Players per match: 1 = start immediately (solo). For LAN games: set SURV_MATCH_SIZE=2
if "%SURV_MATCH_SIZE%"=="" set "SURV_MATCH_SIZE=1"
for /f %%t in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set "STAMP=%%t"
if exist "%DOCS%\survarium_%USERNAME%.log" copy /y "%DOCS%\survarium_%USERNAME%.log" "%LOGS%\client-before-%STAMP%.log" >nul
rem (no spaces in these paths, so no inner quotes inside cmd /k)
start "survarium poc server" cmd /k python -u %HERE%\survarium_poc_server.py --ssl-dir %GAME%\resources\ssl --match-size %SURV_MATCH_SIZE% 2^>^&1 ^| python -u %HERE%\tools\tee.py %LOGS%\server-%STAMP%.log
timeout /t 2 /nobreak >nul
pushd "%GAME%\binaries\win32"
echo %EXE% started %STAMP% > "%LOGS%\last-run.txt"
start "" /wait %EXE% -no_splash_screen -client=127.0.0.1:25100 %2 %3 %4
popd
if exist "%DOCS%\survarium_%USERNAME%.log" copy /y "%DOCS%\survarium_%USERNAME%.log" "%LOGS%\client-%EXE%-%STAMP%.log" >nul
for %%f in ("%DOCS%\*_error_report_*.log") do move /y "%%f" "%LOGS%\" >nul
for %%f in ("%DOCS%\*_error_report_*.dmp") do move /y "%%f" "%LOGS%\" >nul
echo Logs saved in %LOGS%
