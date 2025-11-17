@echo off
setlocal enabledelayedexpansion

:: This script compiles resources and translation files.
:: It assumes you have the QGIS environment (and thus pyrcc5, pylupdate5, lrelease) in your PATH.
:: Run this from the OSGeo4W Shell.

echo Compiling resource file (resources.qrc)...
pyrcc6 -o "resources.py" "resources.qrc"

echo.
echo Creating/updating translation source file (i18n/de.ts)...
if not exist i18n mkdir i18n

:: Find all .py files (excluding specified ones) and build a space-separated
:: list of quoted paths directly into the PY_FILES variable.
set "PY_FILES="
for /f "delims=" %%i in ('dir /s /b *.py ^| findstr /V /I /C:"__pycache__" /C:"\.git" /C:"\.venv" /C:"release.py" /C:"resources.py"') do (
    set "PY_FILES=!PY_FILES! "%%i""
)
pylupdate6 -noobsolete -verbose !PY_FILES! -ts i18n/de.ts

echo.
echo Compiling translation file (i18n/de.qm)...
lrelease i18n/de.ts

echo.
echo Compilation finished.
endlocal