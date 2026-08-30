@echo off
REM Local start. DRY_RUN=1 means the dispatcher records what it WOULD send and
REM never touches the network. Set DRY_RUN=0 only when you mean to dial.
cd /d "%~dp0"
if not defined DRY_RUN set DRY_RUN=1
if not defined LEADS_SOURCE set LEADS_SOURCE=seed

if not exist redial.db (
  echo Seeding redial.db ...
  python -m engine.seed || exit /b 1
)

python -m uvicorn api.main:app --port 8000 --reload
