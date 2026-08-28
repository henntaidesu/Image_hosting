@echo off
setlocal

set "ENV_NAME=picture-bed"
set "PROJECT_DIR=%~dp0"
set "CONDA_BAT=%USERPROFILE%\anaconda3\condabin\conda.bat"

if not exist "%CONDA_BAT%" (
    echo [ERROR] Conda was not found: %CONDA_BAT%
    echo Please install Anaconda/Miniconda or edit CONDA_BAT in this file.
    pause
    exit /b 1
)

call "%CONDA_BAT%" activate "%ENV_NAME%"
if errorlevel 1 (
    echo [ERROR] Conda environment "%ENV_NAME%" is unavailable.
    echo Run: conda create -n %ENV_NAME% python=3.13 -y
    pause
    exit /b 1
)

cd /d "%PROJECT_DIR%"
echo Picture Bed is starting in secure public-listen mode on 0.0.0.0:9990
python app.py

if errorlevel 1 pause
