# Bash Terminal UI Standard

This document is the project-specific implementation of the user's globally
preferred Bash presentation style. Read it before offering shell scripts,
especially for production work. It governs presentation, not permission to
operate: user approval and evidence are separate requirements.

## Visual requirements

- One complete copy-pasteable Bash script, rather than fragmented commands.
- Title: PROJECT · OPERATION with a heavy separator:
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- CYAN (ANSI 36): headline and numbered stages such as ▶ 1/5 · PRECHECK.
- GREEN (ANSI 32): verified ✓ PASS and final ✓ SUCCESS / ✓ RELEASE OK.
- RED (ANSI 31): ✗ FAIL and ✗ FAILED / ✗ RELEASE NOT CONFIRMED.
- YELLOW (ANSI 33): ⚠ WARN and the final interactive prompt.
- RESET (ANSI 0): always restore default terminal color after each message.
- Human-readable Russian stage labels, with exact technical names unchanged.
- Only use colors when stdout is a TTY, TERM is not dumb and NO_COLOR is unset.
- Always conclude with a dedicated ▶ PROJECT · ИТОГ block showing **real**
  results, actual versions and service/data state as appropriate.
- After success OR failure, show in yellow:
  Нажмите Enter для завершения...
  Read it from /dev/tty. Do not block in CI/noninteractive environments.

## Safety and execution contract

- State intended changes and risks *before* the script.
- Use set -Eeuo pipefail and umask 077 for appropriate multi-step scripts;
  quote paths, use least-privilege sudo, avoid unsafe deletion and leaks.
- Preflight: exact Git SHA, branch, clean worktree, binaries, permissions,
  service health, backup integrity, disk capacity and dependencies where needed.
- For production: precheck → backup → isolated tests and trial migrations →
  targeted change → independent verification. No unrelated VPN service restart.
- Use meaningful error handling and traps. Do not hide errors with || true.
  Cleanup failure cannot turn a failure into success. Do not claim a rollback
  exists unless it has been implemented and validated.
- Error details must not expose credentials or personal content. Never print
  .env secrets or enable set -x on a production system.
- Before sharing: verify Bash syntax, quoting, heredocs and safe rerun
  behavior, and never claim tests were run if they were not.
- Do not use this demo as a deployment checklist: tailor every precheck and
  verification to its actual impact.

## Reference: read-only terminal script

The following sample performs only two local read-only checks. It is the
reference for layout, color behavior, final exit status and interactive prompt.
Its Bash syntax was validated with bash -n. A noninteractive successful run
exited 0; a simulated missing dependency exited 1 with FAILED.

```bash
#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

DONE=0
if [[ -t 1 && ${TERM:-dumb} != dumb && -z ${NO_COLOR:-} ]]; then
  CYAN=$'\033[36m'
  GREEN=$'\033[32m'
  RED=$'\033[31m'
  YELLOW=$'\033[33m'
  RESET=$'\033[0m'
else
  CYAN='' GREEN='' RED='' YELLOW='' RESET=''
fi

line() { printf '%s\n' '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━'; }
step() { printf '\n%s▶ %s%s\n' "$CYAN" "$1" "$RESET"; line; }
pass() { printf '%s✓ PASS%s · %s\n' "$GREEN" "$RESET" "$1"; }
warn() { printf '%s⚠ WARN%s · %s\n' "$YELLOW" "$RESET" "$1"; }
fail() { printf '%s✗ FAIL%s · %s\n' "$RED" "$RESET" "$1" >&2; exit 1; }

finish() {
  local rc=$?
  trap - EXIT
  set +e

  step 'EXAMPLE · ИТОГ'
  if (( rc == 0 && DONE == 1 )); then
    printf '%s✓ SUCCESS%s · все заявленные проверки выполнены\n' "$GREEN" "$RESET"
  else
    printf '%s✗ FAILED%s · проверка не завершена\n' "$RED" "$RESET"
    rc=1
  fi

  if { : </dev/tty; } 2>/dev/null; then
    printf '%sНажмите Enter для завершения...%s' "$YELLOW" "$RESET" >/dev/tty
    IFS= read -r _ </dev/tty || :
    printf '\n'
  fi
  exit "$rc"
}

trap finish EXIT

line
printf '%s EXAMPLE · READ-ONLY CHECK%s\n' "$CYAN" "$RESET"
line

step '1/2 · ПРЕДПРОВЕРКА'
command -v python3 >/dev/null 2>&1 || fail 'python3 отсутствует'
pass 'python3 найден'

step '2/2 · ПРОВЕРКА ВЕРСИИ'
python3 --version || fail 'Не удалось получить версию Python'
pass 'Версия Python получена'

DONE=1
```
