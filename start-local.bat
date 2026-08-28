@echo off
setlocal

rem Local development only. Do not use this script for public deployment.
set "PICTURE_BED_INSECURE_COOKIES=1"
call "%~dp0start.bat"
