"""Telegram bot: collect toma de huellas details once, then fill the police form.

Later it only asks for the captcha, if the site shows one.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import time
from io import BytesIO
from pathlib import Path

from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from fields import (
    PROMPTS,
    REQUIRED,
    format_options,
    mask_doc,
    match_label,
    norm,
    parse_bundle,
    validate_doc_number,
    validate_field,
)
from icp import IcpSession, Result

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
PROFILE = DATA / "profile.json"
CAPTCHA_SECONDS = 240

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("cita")

INTRO = """Привет. Помогу поймать cita на отпечатки после резиденции.

Сначала запишу все данные, потом сама заполню форму toma de huellas на сайте полиции. Пока мест нет, больше ничего не спрашиваю. Напишу только если появится капча — её нужно ввести тебе.

Можно прислать всё одним сообщением:

Провинция: Madrid
Комиссария: любая
Документ: NIE
Номер: Y1234567X
Имя: Ivan Ivanov
Гражданство: Ucrania
Год рождения: 1994
Телефон: 612345678
Почта: ivan@example.com

Или отвечай по одному вопросу.
Сейчас нужна провинция. Например: Madrid, Barcelona, Valencia."""


def welcome(update: Update) -> str:
    user = update.effective_user
    name = (user.first_name or "").strip() if user else ""
    if not name:
        return INTRO
    return INTRO.replace("Привет.", f"Привет, {name}.", 1)


DOC_KEYBOARD = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton("NIE", callback_data="t:nie")],
        [InlineKeyboardButton("Паспорт", callback_data="t:pasaporte")],
        [InlineKeyboardButton("DNI", callback_data="t:dni")],
    ]
)


def env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def interval_seconds() -> int:
    try:
        value = int(os.getenv("CHECK_INTERVAL_SECONDS", "600"))
    except ValueError:
        value = 600
    return max(120, value)


def cooldown_seconds(profile: dict, note: str) -> int:
    folded = note.lower()
    if "слишком много запросов" in folded or "too many requests" in folded:
        profile["interval"] = max(int(profile.get("interval", 600)), 600)
        return 60 * 60
    return max(int(profile.get("interval", 600)) * 2, 300)


def clock(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def rate_limit_text() -> str:
    return (
        "Сайт ограничил запросы с этого компьютера. "
        "Проверки стоят час, потом зайду примерно раз в 10 минут. "
        "Не открывай страницу записи и не жми /start — каждый заход продлевает блок."
    )


def headless() -> bool:
    return env_flag("HEADLESS", True)


def empty_profile() -> dict:
    return {
        "owner": None,
        "phase": "quiz",
        "watching": False,
        "peeked": False,
        "peek_error": "",
        "office_options": [],
        "tramite_choices": [],
        "answers": {},
        "correcting": "",
        "interval": interval_seconds(),
        "next_check": 0,
        "misses": 0,
        "captcha_until": 0,
        "last_note": "",
    }


def load_profile() -> dict:
    import json

    if not PROFILE.exists():
        return empty_profile()
    data = json.loads(PROFILE.read_text(encoding="utf-8"))
    base = empty_profile()
    base.update(data)
    if not isinstance(base.get("answers"), dict):
        base["answers"] = {}
    return base


def save_profile(profile: dict) -> None:
    import json

    DATA.mkdir(exist_ok=True)
    PROFILE.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")


def owned(update: Update, profile: dict) -> bool:
    chat = update.effective_chat
    user_id = chat.id if chat else None
    if profile.get("owner") is None:
        profile["owner"] = user_id
        save_profile(profile)
        return True
    return user_id == profile.get("owner")


def steps_needed(profile: dict) -> list[str]:
    keys = ["province"]
    if len(profile.get("tramite_choices") or []) > 1 and not profile["answers"].get("tramite"):
        keys.append("tramite")
    keys.extend(key for key in REQUIRED if key != "province")
    return keys


def next_missing(profile: dict) -> str | None:
    answers = profile["answers"]
    for key in steps_needed(profile):
        if not answers.get(key):
            return key
    return None


def remember(profile: dict, key: str, raw: str) -> str | None:
    if key == "office" and raw.strip() == "0":
        raw = "любая"
    if key == "office" and profile.get("office_options"):
        picked = pick_from_list(raw, profile["office_options"])
        if picked is not None:
            raw = "любая" if "cualquier" in norm(picked) else picked
        elif raw.strip().isdigit():
            return "Такого номера нет в списке комиссарий."
    if key == "tramite" and profile.get("tramite_choices"):
        picked = pick_from_list(raw, profile["tramite_choices"])
        if picked is None:
            return "Пришли номер trámite из списка."
        raw = picked
    value, error = validate_field(key, raw)
    if error:
        return error
    if key == "doc_number":
        doc_error = validate_doc_number(profile["answers"].get("doc_type", ""), value or "")
        if doc_error:
            return doc_error
    if key == "province" and profile["answers"].get("province") != value:
        profile["peeked"] = False
        profile["peek_error"] = ""
        profile["office_options"] = []
        profile["tramite_choices"] = []
        profile["answers"].pop("office", None)
        profile["answers"].pop("tramite", None)
    profile["answers"][key] = value
    return None


def pick_from_list(raw: str, options: list[str]) -> str | None:
    text = raw.strip()
    if text.isdigit():
        index = int(text)
        if 1 <= index <= len(options):
            return options[index - 1]
        return None
    match = match_label(text, [{"label": item, "value": item} for item in options])
    if match:
        return match["label"]
    if norm(text) in {"любая", "любой", "любое", "cualquier", "все", "без разницы"}:
        for item in options:
            if "cualquier" in norm(item):
                return item
    return None


def summary_text(answers: dict) -> str:
    office = "любая" if answers.get("office") == "cualquier" else answers.get("office")
    doc_names = {"nie": "NIE", "pasaporte": "паспорт", "dni": "DNI"}
    return (
        f"Провинция: {answers.get('province')}\n"
        f"Комиссария: {office}\n"
        f"Документ: {doc_names.get(answers.get('doc_type'), '')} {answers.get('doc_number')}\n"
        f"Имя: {answers.get('full_name')}\n"
        f"Гражданство: {answers.get('nationality')}\n"
        f"Год: {answers.get('birth_year')}\n"
        f"Телефон: {answers.get('phone')}\n"
        f"Почта: {answers.get('email')}\n"
        f"Трамит: {answers.get('tramite') or 'toma de huellas'}\n\n"
        "Дальше сама вхожу без Cl@ve и беру самую раннюю cita.\n"
        "Спрошу только капчу, если она появится.\n"
        "Если опечатка — сразу /stop, потом /forget."
    )


def question_text(profile: dict, key: str) -> str:
    extra = ""
    if key == "office" and profile.get("office_options"):
        extra = "\n\n0. Любая\n" + format_options(profile["office_options"])
    elif key == "tramite" and profile.get("tramite_choices"):
        extra = "\n\n" + format_options(profile["tramite_choices"])
    elif key == "office" and profile.get("peek_error"):
        extra = "\n\nСписок с сайта не загрузился, напиши «любая» или точное название."
    return PROMPTS[key] + extra


def question_markup(key: str):
    if key == "doc_type":
        return DOC_KEYBOARD
    if key == "office":
        return InlineKeyboardMarkup([[InlineKeyboardButton("Любая комиссария", callback_data="o:cualquier")]])
    return None


async def send_question(app: Application, chat_id: int, profile: dict) -> None:
    key = next_missing(profile)
    if key is None:
        await start_search(app, chat_id, profile)
        return
    profile["phase"] = "quiz"
    save_profile(profile)
    await app.bot.send_message(chat_id, question_text(profile, key), reply_markup=question_markup(key))


async def ensure_lists(app: Application, chat_id: int, profile: dict) -> str | None:
    if profile.get("peeked") or not profile["answers"].get("province"):
        return None
    await app.bot.send_message(chat_id, "Открываю сайт и смотрю комиссарии в этой провинции…")
    session = IcpSession(headless())
    try:
        await session.open()
        offices, tramites, status = await session.peek_province(profile["answers"]["province"])
    except Exception as exc:
        message = str(exc).splitlines()[0]
        if "Провинции" in message:
            profile["answers"].pop("province", None)
            save_profile(profile)
            return message
        profile["peeked"] = True
        profile["peek_error"] = message
        save_profile(profile)
        await app.bot.send_message(
            chat_id,
            "Список комиссарий сейчас не открылся. Напиши «любая» или название как на сайте.\n" + message,
        )
        return None
    finally:
        await session.close()
    profile["peeked"] = True
    profile["peek_error"] = ""
    profile["office_options"] = offices
    profile["tramite_choices"] = tramites
    if len(tramites) == 1:
        profile["answers"]["tramite"] = tramites[0]
    elif status == "missing":
        profile["answers"].pop("province", None)
        profile["peeked"] = False
        save_profile(profile)
        return "В этой провинции на сайте нет trámite toma de huellas. Напиши другую провинцию."
    elif status == "waiting":
        await app.bot.send_message(
            chat_id,
            "Форма провинции сейчас без списка комиссарий: мест нет. "
            "Всё равно сохраню данные и буду проверять.",
        )
    office = profile["answers"].get("office")
    if office and office != "cualquier" and offices:
        picked = pick_from_list(office, offices)
        if picked is None:
            profile["answers"].pop("office", None)
            save_profile(profile)
            return "Такой комиссарии в списке нет. Выбери номер или «любая»."
        profile["answers"]["office"] = "cualquier" if "cualquier" in norm(picked) else picked
    save_profile(profile)
    return None


async def accept_text(app: Application, chat_id: int, profile: dict, text: str) -> None:
    bundle = parse_bundle(text)
    if bundle:
        errors = []
        for key in ("province", "tramite", "office", "doc_type", "doc_number", "full_name", "nationality", "birth_year", "phone", "email"):
            if key not in bundle:
                continue
            if key == "doc_number" and "doc_type" in bundle and "doc_type" not in profile["answers"]:
                doc_type, doc_type_error = validate_field("doc_type", bundle["doc_type"])
                if doc_type_error:
                    errors.append(doc_type_error)
                else:
                    profile["answers"]["doc_type"] = doc_type
            error = remember(profile, key, bundle[key])
            if error:
                errors.append(error)
        save_profile(profile)
        if errors:
            await app.bot.send_message(chat_id, "Поправь и пришли ещё раз:\n" + "\n".join(errors))
            return
    else:
        key = next_missing(profile) or "province"
        error = remember(profile, key, text)
        save_profile(profile)
        if error:
            await app.bot.send_message(chat_id, error)
            return
    problem = await ensure_lists(app, chat_id, profile)
    if problem:
        await app.bot.send_message(chat_id, problem)
    await send_question(app, chat_id, profile)


async def start_search(app: Application, chat_id: int, profile: dict) -> None:
    doc_error = validate_doc_number(profile["answers"].get("doc_type", ""), profile["answers"].get("doc_number", ""))
    if doc_error:
        profile["answers"].pop("doc_number", None)
        save_profile(profile)
        await app.bot.send_message(chat_id, doc_error)
        await send_question(app, chat_id, profile)
        return
    profile["phase"] = "run"
    profile["watching"] = True
    profile["misses"] = 0
    profile["next_check"] = time.time()
    profile["correcting"] = ""
    save_profile(profile)
    await app.bot.send_message(chat_id, "Данные записала, начинаю.\n\n" + summary_text(profile["answers"]))
    await run_attempt(app, chat_id)


async def run_attempt(app: Application, chat_id: int) -> None:
    if app.bot_data.get("busy"):
        return
    profile = load_profile()
    if profile.get("phase") not in {"run", "captcha"}:
        return
    app.bot_data["busy"] = True
    session = app.bot_data.get("session")
    created = False
    try:
        if session is None or session.page is None:
            show = bool(profile.get("show_once"))
            if show:
                profile["show_once"] = False
                save_profile(profile)
                await app.bot.send_message(
                    chat_id,
                    "Один проход открываю в Chrome на этом компьютере. Смотри окно: страницы по-испански. Потом снова уберу его с экрана.",
                )
            session = IcpSession(headless(), visible=show)
            created = True
            app.bot_data["session"] = session
            await session.open()
            result = await session.drive(profile["answers"])
        else:
            result = await session.drive(profile["answers"])
        while result.kind == "need_cert":
            await app.bot.send_message(chat_id, result.message)
            if not await session.wait_past_certificate():
                result = Result(
                    "error",
                    "Не дождалась выбора сертификата на компьютере. Проверю снова позже.",
                )
                break
            result = await session.drive(profile["answers"])
        await handle_result(app, chat_id, result, keep_session=created or session is not None)
    except Exception as exc:
        log.exception("attempt failed")
        profile = load_profile()
        if profile.get("watching"):
            note = str(exc).splitlines()[0]
            profile["last_note"] = note
            profile["next_check"] = time.time() + cooldown_seconds(profile, note)
            save_profile(profile)
            if "слишком много запросов" in note.lower() or "too many requests" in note.lower():
                await app.bot.send_message(chat_id, rate_limit_text())
            else:
                await app.bot.send_message(chat_id, "Не смогла открыть форму.\n" + note)
        await drop_session(app)
    finally:
        app.bot_data["busy"] = False


async def handle_result(app: Application, chat_id: int, result, keep_session: bool) -> None:
    profile = load_profile()
    kind = result.kind
    if kind == "cancelled":
        await drop_session(app)
        return
    if kind == "captcha":
        profile["phase"] = "captcha"
        profile["watching"] = True
        profile["captcha_until"] = time.time() + CAPTCHA_SECONDS
        profile["last_note"] = result.message
        save_profile(profile)
        await send_captcha(app, chat_id, result)
        return
    await drop_session(app)
    if kind == "no_citas":
        profile["phase"] = "run"
        profile["watching"] = True
        profile["misses"] = int(profile.get("misses", 0)) + 1
        profile["next_check"] = time.time() + profile.get("interval", 120) + random.randint(10, 35)
        profile["last_note"] = "мест нет"
        save_profile(profile)
        await app.bot.send_message(
            chat_id,
            f"Мест нет. Следующая проверка около {clock(profile['next_check'])}.",
        )
        return
    if kind == "booked":
        profile["phase"] = "idle"
        profile["watching"] = False
        profile["last_note"] = "записана"
        save_profile(profile)
        await app.bot.send_message(chat_id, "Cita оформлена. Сохрани номер:\n\n" + result.message)
        if result.page_image:
            await app.bot.send_photo(chat_id, BytesIO(result.page_image))
        return
    if kind == "bad_field" and result.field:
        profile["phase"] = "quiz"
        profile["watching"] = False
        profile["correcting"] = result.field
        profile["answers"].pop(result.field, None)
        profile["last_note"] = result.message
        save_profile(profile)
        await app.bot.send_message(
            chat_id,
            "Сайт не принял данные, форму остановила.\n"
            + result.message
            + "\n\nПришли правильное значение. Остальное заново не спрашиваю.",
        )
        return
    note = result.message or "Ошибка сайта."
    profile["phase"] = "run"
    profile["watching"] = True
    profile["next_check"] = time.time() + cooldown_seconds(profile, note)
    profile["last_note"] = note[:300]
    save_profile(profile)
    if "слишком много запросов" in note.lower() or "too many requests" in note.lower():
        await app.bot.send_message(chat_id, rate_limit_text())
        return
    await app.bot.send_message(chat_id, "Проверка остановилась, повторю позже.\n" + note[:1500])


async def send_captcha(app: Application, chat_id: int, result) -> None:
    text = (
        "Нашла cita. Сессия живёт около 4 минут.\n"
        "Пришли символы с картинки как есть, без пробелов.\n"
        "Если дата не подходит — напиши «мимо», отпущу её и буду искать дальше.\n"
    )
    if result.message:
        text += "\n" + result.message[:900]
    await app.bot.send_message(chat_id, text)
    if result.page_image:
        await app.bot.send_photo(chat_id, BytesIO(result.page_image), caption="Страница записи")
    if result.image:
        await app.bot.send_photo(chat_id, BytesIO(result.image), caption="Капча")
    elif not result.page_image:
        await app.bot.send_message(chat_id, "Картинку капчи не вырезала. Посмотри окно Chrome и пришли символы.")


async def drop_session(app: Application) -> None:
    session = app.bot_data.pop("session", None)
    if session is None:
        return
    if getattr(session, "visible", False) and session.page is not None:
        try:
            await session.page.wait_for_timeout(8000)
        except Exception:
            pass
    await session.close()


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    profile = load_profile()
    if not owned(update, profile):
        await update.message.reply_text("Это личный бот.")
        return
    if profile.get("phase") == "captcha":
        await update.message.reply_text("Сейчас жду капчу по уже найденной cite. Пришли символы или «мимо». /stop — отменить.")
        return
    if profile.get("watching") and profile.get("phase") == "run":
        when = clock(float(profile.get("next_check") or time.time()))
        await update.message.reply_text(
            f"Уже ищу. Следующая проверка около {when}. Повторный /start новую проверку не запускает."
        )
        return
    if next_missing(profile) is None:
        await start_search(context.application, update.effective_chat.id, profile)
        return
    profile["phase"] = "quiz"
    profile["watching"] = False
    save_profile(profile)
    if profile["answers"].get("province"):
        await send_question(context.application, update.effective_chat.id, profile)
        return
    await update.message.reply_text(welcome(update))


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    profile = load_profile()
    if not owned(update, profile):
        return
    profile["watching"] = False
    profile["phase"] = "idle"
    profile["captcha_until"] = 0
    save_profile(profile)
    session = context.application.bot_data.get("session")
    if session is not None:
        session.cancel = True
    if not context.application.bot_data.get("busy"):
        await drop_session(context.application)
    await update.message.reply_text("Остановила. Данные на месте. Снова искать: /start")


async def cmd_forget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    profile = load_profile()
    if not owned(update, profile):
        return
    owner = profile.get("owner")
    fresh = empty_profile()
    fresh["owner"] = owner
    save_profile(fresh)
    session = context.application.bot_data.get("session")
    if session is not None:
        session.cancel = True
    if not context.application.bot_data.get("busy"):
        await drop_session(context.application)
    await update.message.reply_text("Удалила сохранённые данные.\n\n" + welcome(update))


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    profile = load_profile()
    if not owned(update, profile):
        return
    answers = profile["answers"]
    office = "любая" if answers.get("office") == "cualquier" else answers.get("office", "—")
    phase = {
        "quiz": "ещё спрашиваю данные",
        "run": "ищу cita" if profile.get("watching") else "пауза",
        "captcha": "жду капчу",
        "idle": "остановлен",
    }.get(profile.get("phase"), profile.get("phase"))
    minutes = max(2, int(profile.get("interval", 120)) // 60)
    text = (
        f"Состояние: {phase}\n"
        f"Проверок без мест: {profile.get('misses', 0)}\n"
        f"Интервал: {minutes} мин\n"
        f"Следующая проверка: {clock(float(profile.get('next_check') or 0)) if profile.get('watching') else '—'}\n"
        f"Провинция: {answers.get('province', '—')}\n"
        f"Комиссария: {office}\n"
        f"Документ: {mask_doc(answers.get('doc_number', ''))}\n"
        f"Имя: {answers.get('full_name', '—')}\n"
        f"Последнее: {profile.get('last_note') or '—'}"
    )
    await update.message.reply_text(text)


async def cmd_interval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    profile = load_profile()
    if not owned(update, profile):
        return
    if not context.args:
        await update.message.reply_text("Пример: /interval 3 — проверять не чаще раза в 3 минуты. Минимум 2.")
        return
    try:
        minutes = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Нужно число минут, минимум 2.")
        return
    if minutes < 2:
        await update.message.reply_text("Минимум 2 минуты, иначе сайт начинает отклонять запросы.")
        minutes = 2
    profile["interval"] = minutes * 60
    save_profile(profile)
    await update.message.reply_text(f"Интервал {minutes} мин.")


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    profile = load_profile()
    if not owned(update, profile):
        return
    data = query.data or ""
    expected = profile.get("correcting") or next_missing(profile)
    if data.startswith("t:"):
        if expected != "doc_type":
            return
        text = data.split(":", 1)[1]
    elif data == "o:cualquier":
        if expected != "office":
            return
        text = "любая"
    else:
        return
    await accept_text(context.application, update.effective_chat.id, profile, text)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    profile = load_profile()
    if not owned(update, profile):
        await update.message.reply_text("Это личный бот.")
        return
    text = (update.message.text or "").strip()
    chat_id = update.effective_chat.id
    if profile.get("phase") == "captcha":
        await take_captcha(context.application, chat_id, profile, text)
        return
    if profile.get("correcting"):
        key = profile["correcting"]
        error = remember(profile, key, text)
        if error:
            await update.message.reply_text(error)
            return
        profile["correcting"] = ""
        save_profile(profile)
        if next_missing(profile) is None:
            await start_search(context.application, chat_id, profile)
        else:
            await send_question(context.application, chat_id, profile)
        return
    if profile.get("phase") == "run" and profile.get("watching"):
        await update.message.reply_text("Ищу cita и сама ввожу сохранённые данные. Напишу, если нужна капча. /stop — остановить.")
        return
    await accept_text(context.application, chat_id, profile, text)


async def take_captcha(app: Application, chat_id: int, profile: dict, text: str) -> None:
    folded = norm(text)
    if folded in {"мимо", "нет", "пропустить", "skip"}:
        profile["phase"] = "run"
        profile["watching"] = True
        profile["captcha_until"] = 0
        profile["next_check"] = time.time() + profile.get("interval", 120)
        save_profile(profile)
        await drop_session(app)
        await app.bot.send_message(chat_id, "Эту cita не беру. Ищу дальше.")
        return
    if re.search(r"[а-яё]", folded) or len(text) > 12 or len(text) < 3:
        await app.bot.send_message(chat_id, "Нужны только символы с картинки, латиницей. Или «мимо».")
        return
    session = app.bot_data.get("session")
    if session is None or session.page is None:
        profile["phase"] = "run"
        profile["captcha_until"] = 0
        profile["next_check"] = time.time()
        save_profile(profile)
        await app.bot.send_message(chat_id, "Сессия сайта уже закрылась. Ищу заново.")
        return
    profile["phase"] = "run"
    profile["captcha_until"] = 0
    save_profile(profile)
    app.bot_data["busy"] = True
    try:
        result = await session.submit_captcha(profile["answers"], text)
        await handle_result(app, chat_id, result, keep_session=True)
    except Exception as exc:
        log.exception("captcha submit failed")
        await drop_session(app)
        profile = load_profile()
        profile["phase"] = "run"
        profile["watching"] = True
        profile["next_check"] = time.time() + profile.get("interval", 120)
        save_profile(profile)
        await app.bot.send_message(chat_id, "Капча не отправилась, ищу заново.\n" + str(exc).splitlines()[0])
    finally:
        app.bot_data["busy"] = False


async def expire_captcha(app: Application, profile: dict) -> None:
    chat_id = profile.get("owner")
    profile["phase"] = "run"
    profile["watching"] = True
    profile["captcha_until"] = 0
    profile["next_check"] = time.time() + profile.get("interval", 120)
    save_profile(profile)
    await drop_session(app)
    if chat_id:
        await app.bot.send_message(chat_id, "Время капчи вышло, сессию закрыла. Ищу дальше.")


async def watch_loop(app: Application) -> None:
    while True:
        await asyncio.sleep(4)
        try:
            profile = load_profile()
            if profile.get("phase") == "captcha":
                if profile.get("captcha_until") and time.time() > profile["captcha_until"] and not app.bot_data.get("busy"):
                    await expire_captcha(app, profile)
                continue
            if not profile.get("watching") or profile.get("phase") != "run":
                continue
            if time.time() < float(profile.get("next_check") or 0):
                continue
            if app.bot_data.get("busy"):
                continue
            owner = profile.get("owner")
            if owner:
                await run_attempt(app, owner)
        except Exception:
            log.exception("watch loop")


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Начать или продолжить"),
            BotCommand("stop", "Остановить поиск"),
            BotCommand("status", "Что сохранено"),
            BotCommand("forget", "Удалить данные"),
            BotCommand("interval", "Интервал проверки в минутах"),
        ]
    )
    asyncio.create_task(watch_loop(app))


async def post_shutdown(app: Application) -> None:
    await drop_session(app)


def main() -> None:
    load_dotenv(ROOT / ".env")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or token.startswith("123456:"):
        raise SystemExit(
            "Нет токена. Скопируй .env.example в .env, вставь токен от @BotFather и запусти снова."
        )
    app = Application.builder().token(token).post_init(post_init).post_shutdown(post_shutdown).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("forget", cmd_forget))
    app.add_handler(CommandHandler("interval", cmd_interval))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("bot is polling")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
