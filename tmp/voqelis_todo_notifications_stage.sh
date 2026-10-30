#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

REPO=/opt/voqelis
DATA=/var/lib/voqelis
PY="$REPO/.venv/bin/python"
PROD=fd513280d9dfa997db6d0364b17607361adedd17
TODO_SHA=fabc09379b569397a3722fa91ef49a1da72cd3fa
ADMIN_SHA=eb720dc4b8f0c3ed3b20215010fa0894cf6bc53b
STAGE=''
WORKTREES=()
SUCCESS=0
RESTARTS=''

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
  local rc=$? w clean=1
  trap - EXIT
  set +e
  for w in "${WORKTREES[@]}"; do
    if git_app worktree remove "$w" >/dev/null 2>&1; then
      pass 'Временный worktree удалён'
    else
      warn "Worktree не удалён: $w"
      clean=0
      rc=1
    fi
  done
  if [[ -n $STAGE ]]; then
    if (( SUCCESS && rc == 0 && clean )); then
      if app "$PY" - "$STAGE" <<'PY'
import os
import shutil
import stat
import sys
from pathlib import Path

p = Path(sys.argv[1])
root = Path('/var/lib/voqelis')
if (p.parent != root or not p.name.startswith('staging-reminders.')
        or p.is_symlink() or not p.is_dir()
        or p.stat().st_uid != os.getuid()
        or not stat.S_ISDIR(p.lstat().st_mode)):
    raise RuntimeError('Unsafe stage cleanup path')
shutil.rmtree(p)
PY
      then
        pass 'Временные базы и код удалены'
      else
        rc=1
        warn "Не удалось очистить каталог: $STAGE"
      fi
    else
      warn "Временные базы сохранены для проверки: $STAGE"
    fi
  fi
  step 'ИТОГ'
  if (( rc == 0 && SUCCESS == 1 )); then
    printf '%s✓ SUCCESS%s · независимые миграции проверены на копиях\n' "$GREEN" "$RESET"
  else
    printf '%s✗ FAILED%s · релиз НЕ подтверждён; production не обновлялся\n' "$RED" "$RESET"
    rc=1
  fi
  if [[ -z ${VOQELIS_STAGE_WRAPPED:-} ]] && { : </dev/tty; } 2>/dev/null; then
    printf '%sНажмите Enter для завершения...%s' "$YELLOW" "$RESET" >/dev/tty
    IFS= read -r _ </dev/tty || :
    printf '\n'
  fi
  exit "$rc"
}
trap finish EXIT

line
printf '%s VOQELIS · TODO + ADMIN NOTIFICATIONS · STAGING%s\n' "$CYAN" "$RESET"
line
warn 'Только репетиция на копиях: Git production, сервисы и реальные базы не изменяются.'

step '1/5 · PRODUCTION И РЕСУРСЫ'
[[ $EUID -eq 0 && -d $REPO/.git && -x $PY ]] || fail 'Недоступны Git / Python / права'
[[ ! -L $DATA && -d $DATA ]] || fail 'Каталог данных не прошёл проверку'
[[ $(git_app rev-parse --verify HEAD) == "$PROD" ]] || fail 'HEAD production отличается'
[[ $(git_app branch --show-current) == feature/planner-v1 ]] || fail 'Неожиданная ветка'
[[ -z $(git_app status --porcelain) ]] || fail 'Рабочее дерево Git не чистое'
systemctl is-active --quiet voqelis.service || fail 'Voqelis.service остановлен'
RESTARTS=$(systemctl show -p NRestarts --value voqelis.service)
[[ -f $DATA/todo.sqlite3 && ! -L $DATA/todo.sqlite3 ]] || fail 'Нет безопасной todo.sqlite3'
[[ $(stat -c %a "$DATA/todo.sqlite3") == 600 ]] || fail 'Некорректные права todo.sqlite3'
free_kib=$(df -Pk "$DATA" | awk 'NR == 2 {print $4}')
[[ $free_kib =~ ^[0-9]+$ && $free_kib -ge 524288 ]] || fail 'Нужно минимум 512 MiB места'
pass "Production ${PROD:0:12}, service active; места достаточно"

step '2/5 · FETCH ДВУХ ЗАКРЕПЛЁННЫХ ВЕТОК'
git_app fetch --no-tags origin \
  'refs/heads/feature/todo-reminders-v2-1-foundation:refs/remotes/origin/feature/todo-reminders-v2-1-foundation' \
  'refs/heads/docs/admin-notifications-rfc:refs/remotes/origin/docs/admin-notifications-rfc' || fail 'Неудачный Git fetch'
[[ $(git_app rev-parse refs/remotes/origin/feature/todo-reminders-v2-1-foundation) == "$TODO_SHA" ]] || fail 'ToDo PR HEAD изменился'
[[ $(git_app rev-parse refs/remotes/origin/docs/admin-notifications-rfc) == "$ADMIN_SHA" ]] || fail 'Admin PR HEAD изменился'
git_app merge-base --is-ancestor "$PROD" "$TODO_SHA" || fail 'ToDo не является продолжением production'
git_app merge-base --is-ancestor "$PROD" "$ADMIN_SHA" || fail 'Admin не является продолжением production'
pass "ToDo SHA ${TODO_SHA:0:12}; Admin SHA ${ADMIN_SHA:0:12}"

step '3/5 · НЕЗАВИСИМЫЕ WORKTREE'
STAGE=$(app mktemp -d -p "$DATA" 'staging-reminders.XXXXXXXX') || fail 'Создание staging не удалось'
app git -C "$REPO" worktree add --detach "$STAGE/todo-source" "$TODO_SHA" || fail 'ToDo worktree не создан'
WORKTREES+=("$STAGE/todo-source")
app git -C "$REPO" worktree add --detach "$STAGE/admin-source" "$ADMIN_SHA" || fail 'Admin worktree не создан'
WORKTREES+=("$STAGE/admin-source")
pass 'Исходники двух PR изолированы от работающего приложения'

step '4/5 · SQLITE V1 → TODO V2.1 НА КОПИИ'
app env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$STAGE/todo-source/src" "$PY" - "$DATA/todo.sqlite3" "$STAGE" <<'PY'
import hashlib
import os
import shutil
import sqlite3
import stat
import sys
from contextlib import closing
from pathlib import Path

from voqelis.todo.store import TodoStore

source, stage = Path(sys.argv[1]), Path(sys.argv[2])
backup, migrated = stage / 'todo-before.sqlite3', stage / 'todo-after.sqlite3'
with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=20)) as src:
    with closing(sqlite3.connect(backup)) as dst:
        src.backup(dst, pages=256, sleep=0.1)
shutil.copyfile(backup, migrated)
os.chmod(backup, 0o600)
os.chmod(migrated, 0o600)

def digest(db, name):
    h = hashlib.sha256()
    for row in db.execute(f'SELECT * FROM "{name}" ORDER BY rowid'):
        h.update(repr(tuple(row)).encode('utf-8'))
        h.update(b'\n')
    return h.hexdigest()

with closing(sqlite3.connect(backup)) as original:
    assert original.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    tables = [r[0] for r in original.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )]
    expected = {name: digest(original, name) for name in tables}

store = TodoStore(migrated)
try:
    db = store.db
    for name in tables:
        if digest(db, name) != expected[name]:
            raise RuntimeError('Changed existing table: ' + name)
    for name in ('todo_reminders', 'todo_reminder_drafts'):
        if db.execute('SELECT COUNT(*) FROM "' + name + '"').fetchone()[0] != 0:
            raise RuntimeError('New reminder table is not empty: ' + name)
    if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        raise RuntimeError('ToDo V2.1 migration corrupted SQLite')
finally:
    store.close()
if any(stat.S_IMODE(p.stat().st_mode) != 0o600 for p in (backup, migrated)):
    raise RuntimeError('Unsafe temporary database permissions')
print('✓ PASS · Все таблицы ToDo V1 сохранили данные; новая схема и integrity OK')
PY
pass 'Миграция ToDo протестирована без изменения рабочей базы'

step '5/5 · НОВАЯ БАЗА АДМИН-РАССЫЛОК И ФИНАЛ'
app env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$STAGE/admin-source/src" "$PY" - "$STAGE/admin-notifications.sqlite3" <<'PY'
import sqlite3
import stat
import sys
from pathlib import Path
from voqelis.notifications.store import AnnouncementStore

path = Path(sys.argv[1])
assert not path.exists()
store = AnnouncementStore(path, owner_id=1)
try:
    expected = {'announcements', 'announcement_inputs',
                'announcement_deliveries', 'announcement_optouts'}
    actual = {r[0] for r in store.db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if not expected.issubset(actual):
        raise RuntimeError('Missing announcement tables')
    if store.db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        raise RuntimeError('Announcement DB failed integrity check')
    if store.latest(2):
        raise RuntimeError('Guest gained admin access')
    if any(store.db.execute('SELECT COUNT(*) FROM "' + t + '"').fetchone()[0]
           for t in expected):
        raise RuntimeError('New DB must contain no users or announcements')
finally:
    store.close()
if stat.S_IMODE(path.stat().st_mode) != 0o600:
    raise RuntimeError('Unsafe permissions on notification DB')
print('✓ PASS · Новая приватная база рассылок: схема, 0600, owner-only, integrity')
PY
[[ $(git_app rev-parse HEAD) == "$PROD" ]] || fail 'HEAD production изменился'
[[ -z $(git_app status --porcelain) ]] || fail 'Рабочее дерево изменено'
systemctl is-active --quiet voqelis.service || fail 'Сервис Voqelis остановился'
[[ $(systemctl show -p NRestarts --value voqelis.service) == "$RESTARTS" ]] || fail 'Обнаружен рестарт Voqelis'
pass 'ToDo и админ-база проверены; production активен, рестартов нет'
warn 'Реальные Telegram-отправки, production deploy и merge PR НЕ выполнялись.'
SUCCESS=1
