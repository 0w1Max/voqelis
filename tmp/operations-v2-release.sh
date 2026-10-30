#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

PROJECT='VOQELIS · OPERATIONS V2 · КОНТРОЛИРУЕМЫЙ РЕЛИЗ'
REPO=/opt/voqelis
DATA=/var/lib/voqelis
PY="$REPO/.venv/bin/python"
EXPECTED=fd513280d9dfa997db6d0364b17607361adedd17
TARGET=18fe69689019b0c26d4f2178f09f4e2957dfa3cd
BACKUP_ID=''
STAGE=''
WORKTREE=''
WORKTREE_CREATED=0
SWITCH_STARTED=0
SUCCESS=0
READY=0

if [[ -t 1 && ${TERM:-dumb} != dumb && -z ${NO_COLOR:-} ]]; then
  CYAN=$'\033[36m'; GREEN=$'\033[32m'; RED=$'\033[31m'
  YELLOW=$'\033[33m'; RESET=$'\033[0m'
else
  CYAN=''; GREEN=''; RED=''; YELLOW=''; RESET=''
fi
line() { printf '%s\n' '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━'; }
step() { printf '\n%s▶ %s%s\n' "$CYAN" "$1" "$RESET"; line; }
pass() { printf '%s✓ PASS%s · %s\n' "$GREEN" "$RESET" "$1"; }
warn() { printf '%s⚠ WARN%s · %s\n' "$YELLOW" "$RESET" "$1"; }
fail() { printf '%s✗ FAIL%s · %s\n' "$RED" "$RESET" "$1" >&2; exit 1; }
app() { sudo -u voqelis env -u SSH_AUTH_SOCK "$@"; }
git_app() { app git -C "$REPO" "$@"; }

finish() {
  local rc=$? cleaned=1 actual='UNKNOWN' service='UNKNOWN'
  trap - EXIT
  set +e
  if [[ -d $REPO/.git ]]; then
    actual=$(git_app rev-parse --short=12 HEAD 2>/dev/null) || actual='UNAVAILABLE'
  fi
  service=$(systemctl is-active voqelis.service 2>/dev/null) || :
  if [[ -n $WORKTREE && $WORKTREE_CREATED == 1 ]]; then
    if app git -C "$REPO" worktree remove "$WORKTREE" >/dev/null 2>&1; then
      pass 'Временный Git worktree удалён'
    else
      warn "Worktree сохранён: $WORKTREE"
      cleaned=0
      rc=1
    fi
  fi
  if [[ -n $STAGE ]]; then
    if (( rc == 0 && SUCCESS == 1 && cleaned == 1 )); then
      if app "$PY" - "$STAGE" <<'PY'
import os
import shutil
import stat
import sys
from pathlib import Path
p = Path(sys.argv[1])
root = Path('/var/lib/voqelis')
if (p.parent != root or not p.name.startswith('ops-v2-release.')
        or p.is_symlink() or not p.is_dir()
        or p.stat().st_uid != os.getuid()
        or not stat.S_ISDIR(p.lstat().st_mode)):
    raise RuntimeError('Unsafe staging cleanup path')
shutil.rmtree(p)
PY
      then
        pass 'Временные пакеты, исходники и тестовая SQLite удалены'
      else
        warn "Не удалось удалить staging: $STAGE"
        rc=1
      fi
    else
      warn "Материалы staging оставлены для диагностики: $STAGE"
    fi
  fi
  step 'ИТОГ'
  printf 'Git HEAD: %s | voqelis.service: %s\n' "$actual" "$service"
  if [[ -n $BACKUP_ID ]]; then
    printf 'Свежий комплект backup: %s (7 SQLite)\n' "$BACKUP_ID"
  else
    warn 'Новый комплект backup не был подтверждён'
  fi
  if (( rc == 0 && SUCCESS == 1 && READY == 1 )); then
    printf '%s✓ RELEASE OK%s · Operations V2 активен; GitHub-экспорт НЕ включён\n' "$GREEN" "$RESET"
    warn 'Осталась ручная приёмка /start, /ops, /report в Telegram.'
  else
    printf '%s✗ RELEASE NOT CONFIRMED%s · автооткат не выполнялся\n' "$RED" "$RESET"
    if (( SWITCH_STARTED )); then
      warn 'Код/схема уже могли измениться. Не откатывай SQLite без анализа новых записей.'
    else
      warn 'Git fast-forward и рестарт сервиса ещё не начинались.'
    fi
    rc=1
  fi
  if [[ -z ${VOQELIS_RELEASE_WRAPPED:-} ]] && { : </dev/tty; } 2>/dev/null; then
    printf '%sНажмите Enter для завершения...%s' "$YELLOW" "$RESET" >/dev/tty
    IFS= read -r _ </dev/tty || :
    printf '\n'
  fi
  exit "$rc"
}
trap finish EXIT

line
printf '%s%s%s\n' "$CYAN" "$PROJECT" "$RESET"
line
warn 'Скрипт обновит ТОЛЬКО voqelis.service, Git-код и недостающие пакеты его .venv.'
warn 'VPN, 3x-ui, vpn-bot, .env и GitHub App не меняются.'

step '1/7 · ПРЕДПРОВЕРКА PRODUCTION'
[[ $EUID -eq 0 && -d $REPO/.git && -x $PY ]] || fail 'Нужны root, Git и Python venv'
command -v timeout >/dev/null || fail 'Команда timeout не найдена'
command -v sudo >/dev/null || fail 'Команда sudo не найдена'
command -v git >/dev/null || fail 'Команда git не найдена'
[[ $(git_app rev-parse --verify HEAD) == "$EXPECTED" ]] || fail 'Неожиданный production HEAD'
[[ $(git_app branch --show-current) == feature/planner-v1 ]] || fail 'Неверная ветка production'
[[ -z $(git_app status --porcelain) ]] || fail 'Нечистое рабочее дерево Git'
systemctl is-active --quiet voqelis.service || fail 'Сервис Voqelis не работает'
[[ -d $DATA && ! -L $DATA ]] || fail 'Небезопасный каталог SQLite'
app "$PY" -m pip --version >/dev/null || fail 'pip не установлен в venv'
app "$PY" -m pip check >/dev/null || fail 'В production venv уже есть конфликты пакетов'
"$PY" - "$REPO/.env" <<'PY'
import sys
from pathlib import Path
from dotenv import dotenv_values
p = Path(sys.argv[1])
if not p.is_file() or p.is_symlink():
    raise RuntimeError('Missing or unsafe .env')
flag = dotenv_values(p).get('OPERATIONS_GITHUB_EXPORT_ENABLED', 'false')
if str(flag).strip().lower() != 'false':
    raise RuntimeError('GitHub export must remain explicitly disabled')
PY
free_kib=$(df -Pk "$DATA" | awk 'NR == 2 {print $4}')
[[ $free_kib =~ ^[0-9]+$ && $free_kib -ge 786432 ]] || fail 'Нужно минимум 768 MiB свободного места'
pass "Рабочая версия ${EXPECTED:0:12}; экспорт OFF; 768+ MiB свободного места"

step '2/7 · GIT FETCH, ФИКСИРОВАННЫЙ КОММИТ'
git_app fetch --no-tags origin \
  'refs/heads/feature/planner-v1:refs/remotes/origin/feature/planner-v1' || fail 'Git fetch не прошёл'
[[ $(git_app rev-parse origin/feature/planner-v1) == "$TARGET" ]] || fail 'Удалённая ревизия изменилась'
git_app merge-base --is-ancestor "$EXPECTED" "$TARGET" || fail 'Цель не является fast-forward'
STAGE=$(app mktemp -d -p "$DATA" 'ops-v2-release.XXXXXXXX') || fail 'Не создан staging'
WORKTREE="$STAGE/source"
app git -C "$REPO" worktree add --detach "$WORKTREE" "$TARGET" || fail 'Временный worktree не создан'
WORKTREE_CREATED=1
pass "Target HEAD ${TARGET:0:12} доступен в отдельном worktree"

step '3/7 · СВЕЖИЙ ONLINE BACKUP 7/7 SQLITE'
BACKUP_ID=$(app "$PY" - <<'PY'
import os
import shutil
import sqlite3
import stat
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

root = Path('/var/lib/voqelis')
bdir = root / 'backups'
names = ('planner','notes','day_facts','access','navigation','todo','operations')
if root.is_symlink() or bdir.is_symlink():
    raise RuntimeError('Unsafe SQLite backup path')
sources = [root / (name + '.sqlite3') for name in names]
for p in sources:
    if not p.is_file() or p.is_symlink() or stat.S_IMODE(p.stat().st_mode) != 0o600:
        raise RuntimeError('Unsafe or missing database')
bdir.mkdir(parents=True, exist_ok=True, mode=0o700)
if bdir.stat().st_uid != os.getuid() or stat.S_IMODE(bdir.stat().st_mode) & 0o077:
    raise RuntimeError('Backup directory ownership or permissions are unsafe')
needed = sum(p.stat().st_size for p in sources) * 2 + 256 * 1024**2
if shutil.disk_usage(root).free < needed:
    raise RuntimeError('Insufficient space for safe backups')
id_ = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
for name, src in zip(names, sources):
    dst = bdir / f'pre-ops-v2-{id_}-{name}.sqlite3'
    fd = os.open(dst, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
    os.close(fd)
    try:
        with closing(sqlite3.connect(src.as_uri() + '?mode=ro', uri=True, timeout=20)) as a:
            with closing(sqlite3.connect(dst, timeout=20)) as b:
                a.backup(b, pages=256, sleep=0.1)
                if b.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise RuntimeError('Backup integrity failed')
        if dst.stat().st_size == 0 or stat.S_IMODE(dst.stat().st_mode) != 0o600:
            raise RuntimeError('Incomplete backup')
        fd = os.open(dst, os.O_RDONLY|os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except Exception:
        dst.unlink(missing_ok=True)
        raise
fd = os.open(bdir, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
try:
    os.fsync(fd)
finally:
    os.close(fd)
print(id_)
PY
) || fail 'Не удалось завершить backup всех семи SQLite'
[[ -n $BACKUP_ID ]] || fail 'Backup ID не получен'
pass "Создан и проверен комплект 7/7: $BACKUP_ID"

step '4/7 · РЕПЕТИЦИЯ MIGRATION НА СВЕЖЕЙ КОПИИ'
app env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$WORKTREE/src" "$PY" - \
  "$DATA/backups/pre-ops-v2-$BACKUP_ID-operations.sqlite3" \
  "$STAGE/operations-migrated.sqlite3" <<'PY'
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from voqelis.operations.store import OperationsStore

source, dst = map(Path, sys.argv[1:3])
if dst.exists():
    raise RuntimeError('Migration test target exists')
shutil.copyfile(source, dst)
os.chmod(dst, 0o600)
tables = ('ops_config','ops_events','ops_report_requests','ops_reports')
def content_hash(db, table, columns):
    h = hashlib.sha256()
    cols = ','.join('"' + c.replace('"','""') + '"' for c in columns)
    for row in db.execute(f'SELECT {cols} FROM "{table}" ORDER BY rowid'):
        h.update(json.dumps(tuple(row), ensure_ascii=False, default=str).encode('utf-8'))
        h.update(b'\n')
    return h.hexdigest()
with closing(sqlite3.connect(source.as_uri()+'?mode=ro', uri=True)) as old:
    if old.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        raise RuntimeError('Source backup corrupt')
    schema = {t: [r[1] for r in old.execute(f'PRAGMA table_info("{t}")')]
              for t in tables}
    if any(not cols for cols in schema.values()):
        raise RuntimeError('Legacy table missing')
    fingerprints = {t: content_hash(old, t, schema[t]) for t in tables}
obj = OperationsStore(dst, owner_id=1)
try:
    db = obj.db
    for t in tables:
        if content_hash(db,t,schema[t]) != fingerprints[t]:
            raise RuntimeError('Existing operations data changed')
    if 'error_code' not in {r[1] for r in db.execute('PRAGMA table_info(ops_events)')}:
        raise RuntimeError('Missing error_code migration')
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ops_exports'").fetchone() is None:
        raise RuntimeError('Missing ops_exports table')
    if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        raise RuntimeError('Migrated database integrity failed')
finally:
    obj.close()
print('MIGRATION_OK')
PY
pass 'Operations V2 схема и сохранность существующих данных подтверждены'

step '5/7 · БИНАРНЫЕ DEPENDENCIES / PIP CHECK'
app mkdir -p "$STAGE/wheels"
app "$PY" -m pip --isolated download --disable-pip-version-check \
  --no-cache-dir --only-binary=:all: --timeout 15 --retries 2 \
  --dest "$STAGE/wheels" 'PyJWT[crypto]==2.10.1' 'cryptography==50.0.2' || fail 'Не удалось загрузить бинарные wheels'
warn 'Следующее действие установит PyJWT/cryptography и недостающие зависимости в production venv.'
app "$PY" -m pip --isolated install --no-index --no-cache-dir \
  --find-links "$STAGE/wheels" \
  'PyJWT[crypto]==2.10.1' 'cryptography==50.0.2' || fail 'Установка пакетов завершилась ошибкой'
app "$PY" -m pip check >/dev/null || fail 'После установки обнаружены конфликты зависимостей'
app env PYTHONPATH="$WORKTREE/src" "$PY" - <<'PY'
import jwt
import cryptography
from voqelis.operations.github import GitHubAppConfig
assert jwt.__version__ == '2.10.1'
assert cryptography.__version__ == '50.0.2'
assert GitHubAppConfig.from_env() is None
print('Installed PyJWT=', jwt.__version__, 'cryptography=', cryptography.__version__)
PY
pass 'Проверены PyJWT 2.10.1, cryptography 50.0.2 и отключённый GitHub-экспорт'

step '6/7 · РУЧНОЕ ПОДТВЕРЖДЕНИЕ И FAST-FORWARD'
warn 'СЕЙЧАС будет обновлён Git HEAD и перезапущен ТОЛЬКО voqelis.service.'
warn "Семь свежих backup-файлов сохранены: $BACKUP_ID"
if ! { : </dev/tty; } 2>/dev/null; then
  fail 'Нет интерактивного /dev/tty для подтверждения production-релиза'
fi
printf '%sДля запуска релиза напиши DEPLOY и нажми Enter: %s' "$YELLOW" "$RESET" >/dev/tty
IFS= read -r approval </dev/tty || fail 'Подтверждение не получено'
[[ $approval == DEPLOY ]] || fail 'Релиз отменён пользователем'
[[ $(git_app rev-parse HEAD) == "$EXPECTED" && -z $(git_app status --porcelain) ]] || fail 'Перед релизом Git состояние изменилось'
SWITCH_STARTED=1
START_TIME=$(date -u +'%Y-%m-%d %H:%M:%S UTC')
OLD_PID=$(systemctl show voqelis.service -p MainPID --value)
git_app merge --ff-only "$TARGET" || fail 'Git fast-forward не выполнен'
[[ $(git_app rev-parse HEAD) == "$TARGET" ]] || fail 'Git HEAD после обновления неверный'
timeout 120s systemctl restart voqelis.service || fail 'systemctl restart не завершился успешно'
pass 'Git fast-forward выполнен и команда systemctl restart успешна'

step '7/7 · НЕЗАВИСИМАЯ ПРОВЕРКА READINESS'
[[ $(git_app rev-parse HEAD) == "$TARGET" ]] || fail 'Неверный Git HEAD после запуска'
[[ -z $(git_app status --porcelain) ]] || fail 'Git working tree изменился'
for (( attempt=1; attempt<=40; attempt++ )); do
  systemctl is-active --quiet voqelis.service || fail 'Сервис остановился после перезапуска'
  pid=$(systemctl show voqelis.service -p MainPID --value)
  [[ $pid =~ ^[1-9][0-9]*$ ]] || fail 'Нет MainPID'
  [[ $pid != "$OLD_PID" ]] || fail 'PID не изменился при рестарте'
  logs=$(journalctl -u voqelis.service --since "$START_TIME" \
    --no-pager --output=cat -n 200 2>/dev/null) || fail 'Нет доступа к журналу'
  if [[ $logs == *'GigaAM worker is ready'* && $logs == *'Run polling for bot'* ]]; then
    READY=1
    break
  fi
  sleep 3
done
(( READY == 1 )) || fail 'Не подтверждены готовность GigaAM и Telegram polling за 120 секунд'
"$PY" - "$DATA" <<'PY'
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
root=Path(sys.argv[1]); names=('planner','notes','day_facts','access','navigation','todo','operations')
for name in names:
    path=root/(name+'.sqlite3')
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=10)) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
            raise RuntimeError('Integrity check failed: '+name)
with closing(sqlite3.connect((root/'operations.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ops_exports'").fetchone() is None:
        raise RuntimeError('Operations V2 migration not present')
print('Seven production SQLite: integrity OK; ops_exports present')
PY
systemctl show voqelis.service -p ActiveState -p MainPID -p NRestarts --no-pager
pass "Bot active, GigaAM ready, Telegram polling started; HEAD ${TARGET:0:12}; SQLite 7/7 OK"
SUCCESS=1
