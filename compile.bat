@echo off
setlocal

:: This script compiles translation files and can launch Qt Linguist.
:: Run this from the OSGeo4W Shell.

if not defined OSGEO4W_ROOT set "OSGEO4W_ROOT=C:\OSGeo4W"

echo.
echo Creating/updating translation source file (i18n/de.ts)...
if not exist i18n mkdir i18n

call :set_qt_env
echo Using %PYLUPDATE%...

setlocal enabledelayedexpansion
set "PY_FILES="
for /f "delims=" %%i in ('dir /s /b *.py ^| findstr /V /I /C:"__pycache__" /C:"\.git" /C:"\.venv" /C:"release.py"') do (
    set "PY_FILES=!PY_FILES! "%%i""
)
%PYLUPDATE% %FLAGS% !PY_FILES! %TS_FLAG% i18n/de.ts
endlocal

echo.
echo Launching Qt Linguist...
echo Please edit translations, save, and close Linguist to continue...
start /wait linguist i18n/de.ts

echo.
echo Compiling translation file (i18n/de.qm)...
lrelease i18n/de.ts

echo.
echo Translation updated successfully.
goto :eof

:: --- Environment Setup Subroutine ---
:set_qt_env
    :: Avoid re-running if already set
    if defined PYLUPDATE goto :eof

    :: Detect pylupdate version and set environment
    where pylupdate6 >nul 2>nul
    if %ERRORLEVEL% EQU 0 (
        set PYLUPDATE=pylupdate6
        set "PATH=%OSGEO4W_ROOT%\apps\Qt6\bin;%PATH%"
        set "QT_PLUGIN_PATH=%OSGEO4W_ROOT%\apps\Qt6\plugins"
        set "FLAGS=--no-obsolete"
        set "TS_FLAG=--ts"
    ) else (
        set PYLUPDATE=pylupdate5
        set "PATH=%OSGEO4W_ROOT%\apps\Qt5\bin;%PATH%"
        set "QT_PLUGIN_PATH=%OSGEO4W_ROOT%\apps\Qt5\plugins"
        set "FLAGS=-noobsolete"
        set "TS_FLAG=-ts"
    )
    goto :eof