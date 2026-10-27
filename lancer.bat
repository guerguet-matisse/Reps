@echo off
chcp 65001 >nul
title Reps
cd /d "%~dp0"

if not exist reps.py (
  echo Le fichier reps.py est introuvable dans ce dossier.
  echo Mets lancer.bat et reps.py dans le meme dossier.
  pause
  exit /b 1
)

where py >nul 2>nul && (set PY=py) || (set PY=python)
%PY% --version >nul 2>nul
if errorlevel 1 (
  echo Python n'est pas installe ou pas dans le PATH.
  echo Telecharge-le sur https://www.python.org/downloads/ et coche "Add Python to PATH".
  pause
  exit /b 1
)

echo ==============================
echo    REPS
echo ==============================
echo  1 = Fenetre (application)
echo  2 = Navigateur (version web)
echo.
choice /c 12 /n /m "Ton choix : "
if errorlevel 2 goto web
if errorlevel 1 goto gui

:gui
%PY% reps.py gui
goto fin

:web
%PY% reps.py web
goto fin

:fin
if errorlevel 1 pause
