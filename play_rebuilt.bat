@echo off
rem The exe rebuilt from the vostok decompilation (survarium_rebuilt.exe, with
rem survarium-dx11-win32-gold.pdb beside it so crash reports are symbolized).
rem Logs go to poc-server\logs\.
rem -autologin (dev switch, rebuilt exe only) signs in with the saved user.cfg login,
rem or dev/dev; pass -autologin=name:password for a specific account.
call "%~dp0tools\launch.bat" survarium_rebuilt.exe -autologin
