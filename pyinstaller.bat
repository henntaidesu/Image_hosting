@echo off
setlocal EnableExtensions

set "PROJECT_DIR=%~dp0"
set "ENV_NAME=picture-bed"
set "CONDA_BAT=%USERPROFILE%\anaconda3\condabin\conda.bat"
set "APP_NAME=ImageHosting"
set "RELEASE_DIR=%PROJECT_DIR%Releases\%APP_NAME%"
set "BUILD_DIR=%PROJECT_DIR%build\%APP_NAME%"
set "SPEC_DIR=%PROJECT_DIR%build\spec"

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

python -c "import flask, PIL, pystray, waitress" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Runtime dependencies are missing in "%ENV_NAME%".
    echo Run: python -m pip install -r requirements.txt
    pause
    exit /b 1
)

python -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] PyInstaller is not installed in "%ENV_NAME%".
    echo Run: python -m pip install pyinstaller
    pause
    exit /b 1
)

echo Cleaning previous build output...
if exist "%BUILD_DIR%" rmdir /s /q "%BUILD_DIR%"
if exist "%RELEASE_DIR%" rmdir /s /q "%RELEASE_DIR%"
mkdir "%RELEASE_DIR%"
mkdir "%SPEC_DIR%" 2>nul

echo Packaging %APP_NAME%.exe...
python -m PyInstaller --onefile --windowed --clean --noconfirm ^
    --name "%APP_NAME%" ^
    --icon "%PROJECT_DIR%src\static\favicon.ico" ^
    --add-data "%PROJECT_DIR%src\templates;src\templates" ^
    --add-data "%PROJECT_DIR%src\static;src\static" ^
    --hidden-import "tkinter" ^
    --hidden-import "pystray._win32" ^
    --distpath "%RELEASE_DIR%" ^
    --workpath "%BUILD_DIR%" ^
    --specpath "%SPEC_DIR%" ^
    app.py

if errorlevel 1 (
    echo [ERROR] Packaging failed.
    pause
    exit /b 1
)

echo Cleaning build files...
if exist "%PROJECT_DIR%build" rmdir /s /q "%PROJECT_DIR%build"

echo.
echo ========================================
echo Packaging completed successfully.
echo EXE: %RELEASE_DIR%\%APP_NAME%.exe
echo Data folder: %RELEASE_DIR%\data
echo ========================================
pause
