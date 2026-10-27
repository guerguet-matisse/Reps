@echo off
chcp 65001 >nul
title Creation de Reps.exe
cd /d "%~dp0"

if not exist reps.py (
  echo reps.py est introuvable. Mets build_exe.bat et reps.py dans le meme dossier.
  pause
  exit /b 1
)

where py >nul 2>nul && (set PY=py) || (set PY=python)
%PY% --version >nul 2>nul
if errorlevel 1 (
  echo Python n'est pas installe. Telecharge-le sur https://www.python.org/downloads/
  echo ^(coche "Add Python to PATH"^). Il ne sert que pour CREER l'exe, pas pour l'utiliser.
  pause
  exit /b 1
)

echo [1/2] Installation de PyInstaller...
%PY% -m pip install --upgrade pyinstaller
if errorlevel 1 ( echo Echec de l'installation. & pause & exit /b 1 )

set ICON=
if exist reps.ico set ICON=--icon reps.ico

echo [2/2] Creation de Reps.exe...
%PY% -m PyInstaller --onefile --windowed --clean --noconfirm --exclude-module tkinter --name Reps %ICON% reps.py
if errorlevel 1 ( echo Echec de la creation. & pause & exit /b 1 )

echo.
echo ==> Termine ! Ton application est ici : dist\Reps.exe
echo     Tu peux la copier n'importe ou, meme sur un PC sans Python.
echo.
pause
