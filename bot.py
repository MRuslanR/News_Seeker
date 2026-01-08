# bot.py
import asyncio
import html
import os
import pandas as pd
import shutil
from datetime import time, timezone

from telegram import Update, constants
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    filters,
    ContextTypes,
)

import config
from main import main_cycle

config.setup_logging()
logger = config.get_logger(__name__)

processing_lock = asyncio.Lock()

def escape_html(text: str) -> str:
    return html.escape(str(text))

async def send_long_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str):
    MAX_LENGTH = 4000
    if len(text) <= MAX_LENGTH:
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=constants.ParseMode.HTML)
        return

    parts = []
    current_pos = 0
    while current_pos < len(text):
        end_pos = current_pos + MAX_LENGTH
        part = text[current_pos:end_pos]
        if end_pos < len(text):
            last_newline = part.rfind('\n')
            if last_newline != -1:
                part = part[:last_newline]
                end_pos = current_pos + len(part) + 1
            else:
                last_space = part.rfind(' ')
                if last_space != -1:
                    part = part[:last_space]
                    end_pos = current_pos + len(part) + 1
        parts.append(part)
        current_pos = end_pos

    for part in parts:
        if part.strip():
            await context.bot.send_message(chat_id=chat_id, text=part, parse_mode=constants.ParseMode.HTML)
            await asyncio.sleep(0.5)


async def send_typing_periodically(context: ContextTypes.DEFAULT_TYPE, chat_id: int, stop_event: asyncio.Event):
    while not stop_event.is_set():
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=constants.ChatAction.TYPING)
            await asyncio.wait_for(stop_event.wait(), timeout=4)
        except asyncio.TimeoutError:
            continue
        except (asyncio.CancelledError, Exception):
            break


async def run_processing_job(context: ContextTypes.DEFAULT_TYPE, chat_id: int, trigger_type: str):
    if processing_lock.locked():
        logger.warning("Попытка запуска обработки (%s), когда она уже активна. Запуск отменен.", trigger_type)
        message_text = "⏳ Обработка уже запущена. Дождитесь ее завершения."
        if trigger_type == 'scheduled' and context.job:
            job_name_escaped = escape_html(context.job.name)
            message_text += f"\n\n<i>(Запланированный запуск «{job_name_escaped}» был пропущен)</i>"
        await context.bot.send_message(chat_id=chat_id, text=message_text, parse_mode=constants.ParseMode.HTML)
        return

    async with processing_lock:
        logger.info("Блокировка установлена. Триггер: %s.", trigger_type)
        proc_msg = None
        stop_typing_event = asyncio.Event()
        typing_task = asyncio.create_task(send_typing_periodically(context, chat_id, stop_typing_event))

        try:
            proc_msg = await context.bot.send_message(chat_id=chat_id,
                                                      text=f"🚀 Начинаю обработку новостей... (Запуск: {trigger_type})")
            logger.info("Запуск main_cycle для чата %d", chat_id)

            main_message = await asyncio.to_thread(main_cycle)

            stop_typing_event.set()
            await typing_task

            if not main_message:
                logger.info("Новых новостей для отправки нет. Цикл завершен.")
                await context.bot.send_message(chat_id=chat_id, text="No news for the past period")
            else:
                logger.info("Отправка итогового сообщения в чат %d.", chat_id)
                await send_long_message(context, chat_id, main_message)

        except Exception as e:
            logger.critical("Критическая ошибка в run_processing_job", exc_info=True)
            if not stop_typing_event.is_set(): stop_typing_event.set()
            await typing_task
            try:
                error_text = escape_html(e)
                await context.bot.send_message(chat_id=chat_id,
                                               text=f"❌ <b>Критическая ошибка</b>\n\n<code>{error_text}</code>",
                                               parse_mode=constants.ParseMode.HTML)
            except Exception as send_e:
                logger.error("Не удалось даже отправить сообщение об ошибке: %s", send_e)
        finally:
            if not typing_task.done(): typing_task.cancel()
            if proc_msg:
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=proc_msg.message_id)
                    logger.info("Сообщение о старте обработки удалено.")
                except Exception:
                    logger.warning("Не удалось удалить сообщение о старте.")
            logger.info("Блокировка снята.")


async def start_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if str(chat.id) != config.TELEGRAM_CHAT_ID:
        logger.warning("Получена команда /start в непредусмотренном чате ID: %d", chat.id)
        return
    logger.info("Получена команда /start в чате %d", chat.id)
    asyncio.create_task(run_processing_job(context, chat.id, trigger_type='manual'))


async def scheduled_run(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    chat_id = job.chat_id
    logger.info("Сработал запланированный запуск (job: %s)", job.name)
    await run_processing_job(context, chat_id, trigger_type='scheduled')

'''
def main():
    logger.info("--- Запуск бота ---")
    try:
        config.load_feed_sources()
    except Exception as e:
        logger.critical("Критическая ошибка конфигурации, бот не может быть запущен: %s", e)
        return

    if not config.TELEGRAM_BOT_TOKEN:
        logger.critical("Не указан TELEGRAM_BOT_TOKEN. Бот не может быть запущен.")
        return
    if not config.TELEGRAM_CHAT_ID:
        logger.critical("Не указан TELEGRAM_CHAT_ID. Бот не может быть запущен.")
        return

    app = ApplicationBuilder().token(config.TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.COMMAND & filters.Regex(r'^/start'), start_command_handler))

    schedule_times = config.load_schedule()
    if schedule_times:
        job_queue = app.job_queue
        for t_str in schedule_times:
            try:
                hour, minute = map(int, t_str.split(':'))
                run_time = time(hour, minute, tzinfo=timezone.utc)
                job_queue.run_daily(callback=scheduled_run, time=run_time, chat_id=config.TELEGRAM_CHAT_ID,
                                    name=f"Daily digest at {t_str} UTC")
            except (ValueError, TypeError):
                logger.error("Неверный формат времени в расписании: '%s'. Пропускаем.", t_str)
        if job_queue.jobs():
            logger.info("Всего задач по расписанию добавлено: %d", len(job_queue.jobs()))
    else:
        logger.info("Задачи по расписанию не добавлены.")

    logger.info("Бот запущен и готов к работе.")
    app.run_polling()


if __name__ == "__main__":
    main()'''
    
async def get_excel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправляет текущий файл config.xlsx пользователю."""
    chat = update.effective_chat
    if str(chat.id) != config.TELEGRAM_CHAT_ID:
        return

    if not config.CONFIG_FILEPATH.exists():
        await context.bot.send_message(chat_id=chat.id, text="❌ Файл конфигурации не найден на сервере.")
        return

    try:
        await context.bot.send_document(
            chat_id=chat.id,
            document=config.CONFIG_FILEPATH,
            filename='config.xlsx',
            caption="📂 Текущая конфигурация бота"
        )
        logger.info("Файл конфигурации отправлен пользователю.")
    except Exception as e:
        logger.error("Ошибка при отправке файла конфигурации: %s", e)
        await context.bot.send_message(chat_id=chat.id, text=f"❌ Ошибка отправки: {e}")


def apply_schedule(job_queue):
    """Вспомогательная функция для (пере)загрузки расписания."""
    # 1. Удаляем старые задачи с фиксированным именем
    current_jobs = job_queue.jobs()
    removed_count = 0
    for job in current_jobs:
        if job.name and job.name.startswith("Daily digest"):
            job.schedule_removal()
            removed_count += 1
    
    # 2. Загружаем новое расписание
    schedule_times = config.load_schedule()
    added_count = 0
    if schedule_times:
        for t_str in schedule_times:
            try:
                hour, minute = map(int, t_str.split(':'))
                run_time = time(hour, minute, tzinfo=timezone.utc)
                job_queue.run_daily(
                    callback=scheduled_run, 
                    time=run_time, 
                    chat_id=config.TELEGRAM_CHAT_ID,
                    name=f"Daily digest at {t_str} UTC"
                )
                added_count += 1
            except (ValueError, TypeError):
                logger.error("Неверный формат времени: '%s'", t_str)
    
    return removed_count, added_count


async def update_excel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Принимает файл, проверяет подпись, делает бэкап и обновляет config.xlsx.
    Срабатывает, если админ отправил документ с подписью /update_excel.
    """
    chat = update.effective_chat
    if str(chat.id) != config.TELEGRAM_CHAT_ID:
        return

    document = update.message.document
    if not document.file_name.endswith(('.xlsx', '.xls')):
        await update.message.reply_text("❌ Это не Excel файл.")
        return

    status_msg = await update.message.reply_text("📥 Скачивание и проверка файла...")
    
    # Путь для временного файла
    temp_path = config.BASE_DIR / 'temp_config_upload.xlsx'
    
    try:
        # Скачиваем файл
        new_file = await document.get_file()
        await new_file.download_to_drive(temp_path)

        # ВАЛИДАЦИЯ: Попробуем прочитать pandas
        try:
            pd.read_excel(temp_path, sheet_name='Feeds') # Проверяем критичный лист
        except Exception as e:
            await context.bot.edit_message_text(
                chat_id=chat.id, 
                message_id=status_msg.message_id, 
                text=f"❌ <b>Файл поврежден или имеет неверную структуру:</b>\n<code>{escape_html(e)}</code>",
                parse_mode=constants.ParseMode.HTML
            )
            return

        # БЭКАП: Копируем текущий конфиг
        if config.CONFIG_FILEPATH.exists():
            backup_path = config.CONFIG_FILEPATH.with_suffix('.xlsx.bak')
            shutil.copy(config.CONFIG_FILEPATH, backup_path)
            logger.info("Создан бэкап конфигурации: %s", backup_path)

        # ПЕРЕЗАПИСЬ: (shutil.move перезапишет файл атомарно на POSIX)
        shutil.move(temp_path, config.CONFIG_FILEPATH)
        
        # HOT RELOAD: Обновляем расписание
        removed, added = apply_schedule(context.job_queue)
        
        await context.bot.edit_message_text(
            chat_id=chat.id,
            message_id=status_msg.message_id,
            text=(
                "✅ <b>Конфигурация успешно обновлена!</b>\n"
                f"♻️ Расписание запусков перезагружено.\n"
                f"<i>Списки RSS лент будут подхвачены при следующем запуске.</i>"
            ),
            parse_mode=constants.ParseMode.HTML
        )
        logger.info("Конфигурация обновлена через Telegram.")

    except Exception as e:
        logger.error("Ошибка при обновлении конфига: %s", e, exc_info=True)
        await context.bot.edit_message_text(
            chat_id=chat.id,
            message_id=status_msg.message_id,
            text=f"❌ Внутренняя ошибка при обновлении: {e}"
        )
    finally:
        # Чистим мусор если остался
        if temp_path.exists():
            os.remove(temp_path)

# --- MAIN ---

def main():
    logger.info("--- Запуск бота ---")
    try:
        config.load_feed_sources()
    except Exception as e:
        logger.critical("Критическая ошибка конфигурации, бот не может быть запущен: %s", e)
        return

    if not config.TELEGRAM_BOT_TOKEN:
        logger.critical("Не указан TELEGRAM_BOT_TOKEN.")
        return
    if not config.TELEGRAM_CHAT_ID:
        logger.critical("Не указан TELEGRAM_CHAT_ID.")
        return

    app = ApplicationBuilder().token(config.TELEGRAM_BOT_TOKEN).build()

    # Хендлеры
    app.add_handler(MessageHandler(filters.COMMAND & filters.Regex(r'^/start'), start_command_handler))
    
    # 1. Команда получения конфига /get_excel
    app.add_handler(MessageHandler(filters.COMMAND & filters.Regex(r'^/get_excel'), get_excel_command))
    
    # 2. Хендлер загрузки (Документ + Подпись /update_excel)
    app.add_handler(MessageHandler(
        filters.Document.ALL & filters.CaptionRegex(r'^/update_excel'), 
        update_excel_handler
    ))

    # Первичная настройка расписания
    removed, added = apply_schedule(app.job_queue)
    if added > 0:
        logger.info("Всего задач по расписанию добавлено: %d", added)
    else:
        logger.info("Задачи по расписанию не добавлены.")

    logger.info("Бот запущен и готов к работе.")
    app.run_polling()


if __name__ == "__main__":
    main()