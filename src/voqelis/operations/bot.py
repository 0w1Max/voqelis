"""Telegram reporting and owner-only privacy-safe operations dashboard."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ..access import AccessStore
from ..menu import REPORT_BUTTON
from ..navigation import NavigationStore
from .github import GitHubAppConfig, GitHubExportFailure, GitHubIssuesClient, make_http_client
from .store import OperationsStore, Report

logger = logging.getLogger(__name__)


def _buttons(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data)
         for label, data in row] for row in rows
    ])


def _report_text(report: Report) -> str:
    date = datetime.fromtimestamp(report.created_at, UTC).strftime("%d.%m.%Y %H:%M UTC")
    counts = report.snapshot
    return (
        f"🐞 Отчёт VQ-{report.id} · {report.status}\n"
        f"Время: {date}\n"
        f"Пользователь: Telegram ID {report.user_id}\n"
        f"Период: {report.window_minutes} минут\n"
        "Технические события за период:\n"
        f"• обработано обновлений: {counts.get('handled', 0)}\n"
        f"• завершено аудио: {counts.get('completed', 0)}\n"
        f"• зарегистрировано ошибок: {counts.get('failed', 0)}\n\n"
        f"Комментарий: {report.description or 'Не указан'}\n\n"
        "Это метаданные, а не содержимое переписки. "
        "Отсутствие технических ошибок не гарантирует правильное поведение."
    )


def _admin_keyboard() -> InlineKeyboardMarkup:
    return _buttons([
        [("🐞 Последние отчёты", "ops:reports:1")],
        [("📊 Последние 10 минут", "ops:diag:10"),
         ("30 минут", "ops:diag:30"), ("60 минут", "ops:diag:60")],
        [("👤 Доступ тестировщика", "ops:reporters:0")],
    ])


def _report_keyboard(token: str) -> InlineKeyboardMarkup:
    return _buttons([[
        ("10 минут", f"ops:submit:10:{token}"),
        ("30 минут", f"ops:submit:30:{token}"),
        ("60 минут", f"ops:submit:60:{token}"),
    ]])


def create_operations_router(
    *, store: OperationsStore, access: AccessStore,
    navigation: NavigationStore,
    github_config: GitHubAppConfig | None = None,
    release_sha: str = "unknown",
) -> Router:
    router = Router(name="operations")
    owner = access.admin_id

    def approved(user_id: int) -> bool:
        return access.is_allowed(user_id)

    def admin(user_id: int) -> bool:
        return owner is not None and owner == user_id and approved(user_id)

    def reporter(user_id: int) -> bool:
        return store.eligible(user_id, approved=approved(user_id))

    @router.message(Command("report"))
    @router.message(F.text == REPORT_BUTTON)
    async def on_report(message: Message) -> None:
        if (
            message.from_user is None
            or message.chat.type != ChatType.PRIVATE
            or not reporter(message.from_user.id)
        ):
            return
        raw = message.text or ""
        description = raw.split(maxsplit=1)[1] if raw.startswith("/report ") else ""
        token = store.begin_report(message.from_user.id, description)
        navigation.activate(message.from_user.id, "home")
        await message.answer(
            "🐞 Отчёт об ошибке\n\n"
            "Выбери период, за который собрать события. "
            "Жалоба будет зарегистрирована сразу после выбора.\n\n"
            "Необязательно добавлять пояснение. Если оно нужно, "
            "начни заново командой /report описание проблемы.\n"
            "Содержимое голосовых, фактов и заметок не собирается.",
            reply_markup=_report_keyboard(token),
        )

    @router.message(Command("ops"))
    async def on_admin(message: Message) -> None:
        if (
            message.from_user is None
            or message.chat.type != ChatType.PRIVATE
            or not admin(message.from_user.id)
        ):
            return
        await message.answer(
            "🛠 Операции Voqelis\n"
            f"Тестировщик: {store.reporter_id() or 'не назначен'}\n"
            "Отчёты хранятся только в локальной SQLite. "
            "В GitHub отправляются только обезличенные метаданные по "
            "твоему отдельному подтверждению.\n"
            f"Экспорт: {'включён' if github_config else 'отключён — нужен GitHub App'}.",
            reply_markup=_admin_keyboard(),
        )

    @router.callback_query(F.data.startswith("ops:"))
    async def on_callback(callback: CallbackQuery, bot: object) -> None:
        if callback.message is None or callback.message.chat.type != ChatType.PRIVATE:
            await callback.answer()
            return
        user_id = callback.from_user.id
        parts = (callback.data or "").split(":")
        if len(parts) >= 4 and parts[1] == "submit":
            if not reporter(user_id) or not parts[2].isdigit() or len(parts) != 4:
                await callback.answer("Нет доступа.", show_alert=True)
                return
            report = store.submit_report(user_id, parts[3], int(parts[2]))
            if report is None:
                await callback.answer(
                    "Кнопка устарела или достигнут лимит 3 отчёта в час.",
                    show_alert=True,
                )
                return
            await callback.answer("Отчёт зарегистрирован.")
            await callback.message.answer(
                f"✅ Отчёт VQ-{report.id} сохранён. Администратор увидит его "
                "в разделе «🛠 Операции» → «Последние отчёты»."
            )
            if owner is not None and user_id != owner:
                try:
                    await bot.send_message(
                        owner,
                        f"🐞 Новый отчёт VQ-{report.id} от пользователя "
                        f"{user_id}. Открой /ops для просмотра.",
                    )
                except Exception:
                    logger.exception("OPERATIONS_REPORT_NOTIFICATION_FAILED")
            return

        if not admin(user_id):
            await callback.answer("Только для администратора.", show_alert=True)
            return

        action = parts[1] if len(parts) > 1 else ""
        if action == "admin" and len(parts) == 2:
            await callback.answer()
            await callback.message.answer(
                "🛠 Операции Voqelis",
                reply_markup=_admin_keyboard(),
            )
            return

        if action == "reports" and len(parts) == 3 and parts[2].isdigit():
            page = int(parts[2])
            if not 1 <= page <= 100:
                await callback.answer("Неверная страница.", show_alert=True)
                return
            reports = store.reports(page)
            rows = [
                [(f"VQ-{r.id} · {r.status} · {r.user_id}", f"ops:view:{r.id}")]
                for r in reports
            ]
            pagination: list[tuple[str, str]] = []
            if page > 1:
                pagination.append(("⬅️", f"ops:reports:{page - 1}"))
            if len(reports) == 5 and page < 100:
                pagination.append(("➡️", f"ops:reports:{page + 1}"))
            if pagination:
                rows.append(pagination)
            if not rows:
                rows = [[("🛠 Меню", "ops:admin")]]
            await callback.answer()
            await callback.message.answer(
                f"🐞 Отчёты · страница {page}\n"
                + ("Выбери отчёт." if reports else "Новых записей на странице нет."),
                reply_markup=_buttons(rows),
            )
            return

        if action in {"view", "resolve"} and len(parts) == 3 and parts[2].isdigit():
            report_id = int(parts[2])
            if action == "resolve" and not store.resolve(report_id):
                await callback.answer("Уже обработан или не найден.", show_alert=True)
                return
            report = store.get_report(report_id)
            if report is None:
                await callback.answer("Отчёт не найден.", show_alert=True)
                return
            await callback.answer()
            rows = []
            if report.status == "new":
                rows.append([("✅ Отметить разобранным", f"ops:resolve:{report_id}")])
            rows.append([("👁 Экспорт: предпросмотр", f"ops:preview:{report_id}")])
            markup = _buttons(rows)
            await callback.message.answer(
                _report_text(report), parse_mode=None, reply_markup=markup,
            )
            return


        if action == "preview" and len(parts) == 3 and parts[2].isdigit():
            report_id = int(parts[2])
            ticket = store.prepare_export(user_id, report_id, release_sha)
            if ticket is None:
                await callback.answer("Отчёт не найден.", show_alert=True)
                return
            rows = []
            if ticket.state == "sent":
                status = (
                    f"Уже отправлено: https://github.com/0w1Max/voqelis-ops/"
                    f"issues/{ticket.issue_number}"
                )
            elif ticket.state in {"uncertain", "sending"}:
                status = (
                    "Результат предыдущей отправки неизвестен. "
                    "Повторная отправка запрещена."
                )
                if github_config is not None:
                    rows.append([(
                        "🔎 Сверить с GitHub", f"ops:reconcile:{report_id}",
                    )])
            else:
                status = (
                    "Экспорт отключён: настрой GitHub App."
                    if github_config is None else
                    "Для создания Issue нажми кнопку подтверждения."
                )
                if github_config is not None:
                    rows.append([(
                        "📤 Подтвердить создание Issue",
                        f"ops:send:{report_id}:{ticket.token}",
                    )])
            await callback.answer()
            await callback.message.answer(
                f"👁 Предпросмотр экспорта\n\n{ticket.issue_body}\n\n"
                f"{status}\n\n"
                "Личные комментарии и Telegram ID не передаются.",
                reply_markup=_buttons(rows) if rows else None,
                parse_mode=None,
            )
            return

        if action == "send" and len(parts) == 4 and parts[2].isdigit():
            if github_config is None:
                await callback.answer("GitHub App не настроен.", show_alert=True)
                return
            report_id = int(parts[2])
            ticket = store.claim_export(user_id, report_id, parts[3])
            if ticket is None:
                await callback.answer(
                    "Подтверждение устарело или уже использовано.", show_alert=True,
                )
                return
            await callback.answer("Запрос принят.")
            try:
                async with make_http_client() as client:
                    number = await GitHubIssuesClient(
                        github_config, client,
                    ).create_issue(ticket.marker, ticket.issue_body)
            except GitHubExportFailure as exc:
                store.fail_export(
                    report_id, ticket.marker, exc.code, uncertain=exc.uncertain,
                )
                await callback.message.answer(
                    "⚠ Отправка не подтверждена. "
                    + ("Результат неизвестен, повтор запрещён; открой предпросмотр "
                       "и выбери сверку с GitHub."
                       if exc.uncertain else
                       "GitHub отклонил запрос; проверь конфигурацию и сделай "
                       "новый предпросмотр.")
                    + f"\nКод: {exc.code}"
                )
                return
            except Exception:
                # A failure after dispatch cannot prove that GitHub did not create the Issue.
                store.fail_export(
                    report_id, ticket.marker, "CREATE_OUTCOME_UNKNOWN", uncertain=True,
                )
                logger.error("OPERATIONS_GITHUB_UNEXPECTED_FAILURE")
                await callback.message.answer(
                    "⚠ Результат отправки неизвестен. Повтор запрещён; "
                    "открой предпросмотр и запусти сверку с GitHub."
                )
                return
            store.sent_export(report_id, ticket.marker, number)
            await callback.message.answer(
                f"✅ Создан приватный Issue #{number}\n"
                f"https://github.com/0w1Max/voqelis-ops/issues/{number}"
            )
            return

        if action == "reconcile" and len(parts) == 3 and parts[2].isdigit():
            if github_config is None:
                await callback.answer("GitHub App не настроен.", show_alert=True)
                return
            report_id = int(parts[2])
            ticket = store.get_export(report_id)
            if ticket is None or ticket.state != "uncertain":
                await callback.answer("Нечего сверять.", show_alert=True)
                return
            await callback.answer("Проверяю...")
            try:
                async with make_http_client() as client:
                    number = await GitHubIssuesClient(
                        github_config, client,
                    ).reconcile(ticket.marker)
            except GitHubExportFailure:
                await callback.message.answer(
                    "⚠ GitHub пока недоступен для сверки. Повторное создание "
                    "Issue заблокировано."
                )
                return
            if number is None:
                await callback.message.answer(
                    "⚠ Совпадение пока не найдено. Поиск GitHub может "
                    "индексироваться с задержкой; повторное создание Issue "
                    "остаётся заблокированным."
                )
                return
            if store.sent_export(report_id, ticket.marker, number, reconcile=True):
                await callback.message.answer(
                    f"✅ Найден существующий Issue #{number}\n"
                    f"https://github.com/0w1Max/voqelis-ops/issues/{number}"
                )
            else:
                await callback.message.answer("Состояние изменилось; обнови отчёт.")
            return

        if action == "diag" and len(parts) == 3 and parts[2].isdigit():
            window = int(parts[2])
            if window not in {10, 30, 60}:
                await callback.answer("Неверный период.", show_alert=True)
                return
            counts = store.summary(None, window)
            await callback.answer()
            await callback.message.answer(
                f"📊 События за {window} минут\n"
                f"Обработано обновлений: {counts['handled']}\n"
                f"Завершено аудио: {counts['completed']}\n"
                f"Ошибки: {counts['failed']}\n\n"
                "Не все логические ошибки выявляются автоматически."
            )
            return

        if action == "reporters" and len(parts) == 3 and parts[2].isdigit():
            page = int(parts[2])
            if not 0 <= page <= 100:
                await callback.answer("Неверная страница.", show_alert=True)
                return
            granted = access.granted(limit=10, offset=page * 10)
            rows = [
                [(f"Разрешить #{int(r['user_id'])}", f"ops:grant:{int(r['user_id'])}")]
                for r in granted
            ]
            if store.reporter_id() is not None:
                rows.append([("🚫 Отозвать разрешение", "ops:clear")])
            if len(granted) == 10:
                rows.append([("➡️ Ещё", f"ops:reporters:{page + 1}")])
            if page:
                rows.append([("⬅️ Назад", f"ops:reporters:{page - 1}")])
            await callback.answer()
            await callback.message.answer(
                "👤 Выбери одного одобренного пользователя для кнопки "
                "«🐞 Сообщить об ошибке». Старое разрешение заменится новым.\n"
                f"Сейчас: {store.reporter_id() or 'никто'}\n"
                "После назначения попроси пользователя нажать /start.",
                reply_markup=_buttons(rows or [[("🛠 Меню", "ops:admin")]]),
            )
            return

        if action == "grant" and len(parts) == 3 and parts[2].isdigit():
            target = int(parts[2])
            if not store.set_reporter(
                user_id, target, target_approved=access.is_allowed(target),
            ):
                await callback.answer("Не удалось назначить.", show_alert=True)
                return
            await callback.answer("Доступ назначен.")
            await callback.message.answer(
                f"✅ Пользователю {target} разрешены отчёты. Попроси нажать /start."
            )
            return

        if action == "clear" and len(parts) == 2:
            store.set_reporter(user_id, None, target_approved=True)
            await callback.answer("Разрешение отозвано.")
            await callback.message.answer("Доступ тестировщика отключён.")
            return

        await callback.answer("Неизвестное действие.", show_alert=True)

    return router
