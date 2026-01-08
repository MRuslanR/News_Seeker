# main.py
import sqlite3
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Tuple

from config import (
    OPENROUTER_API_KEY,      
    FILTER_MODEL,        
    FINAL_REPORT_MODEL,
    get_logger, 
    load_feed_sources, 
    MAX_CONCURRENT_WORKERS, 
    MAX_PARSE_HOURS,
    update_excel_with_failures 
)

from prompts import FILTER_SYSTEM_PROMPT, FINAL_REPORT_SYSTEM_PROMPT
from services import OpenRouterClient, NewsFetcher
from utils import escape_html

sqlite3.register_adapter(datetime, lambda ts: ts.isoformat())
sqlite3.register_converter("timestamp", lambda val: datetime.fromisoformat(val.decode()))

logger = get_logger(__name__)

def process_country(
        country_code: str,
        rss_links: List[str],
        news_fetcher: NewsFetcher,
        ai_client: OpenRouterClient,
        start_dt_utc: datetime,
        end_dt_utc: datetime
) -> Tuple[str, str, int, float, List[str], List[str]]:
    """
    Выполняет полный цикл обработки для одной страны.
    Возвращает: (код страны, отчет, токены, цена, ошибки RSS, уведомления RSS).
    """
    log_ctx = {'context': {'country_code': country_code}}
    result = None
    
    fetch_failures, fetch_alerts = [], []
    filtered_report = ""
    ai_tokens = 0
    ai_price = 0.0
    
    try:
        raw_news_text, fetch_failures, fetch_alerts = news_fetcher.fetch_and_process_news(
            country_code=country_code, rss_links=rss_links,
            start_dt_utc=start_dt_utc, end_dt_utc=end_dt_utc
        )

        if not raw_news_text.strip():
            logger.info("Нет новых новостей для обработки AI для %s.", country_code, extra=log_ctx)
            return country_code, "", 0, 0.0, fetch_failures, fetch_alerts

        result = ai_client.create_chat_completion(
            system_prompt=FILTER_SYSTEM_PROMPT, 
            user_content=raw_news_text
        )
        
        ai_tokens = result.get('tokens', 0)
        ai_price = result.get('price', 0.0)
        filtered_report = result.get('result', "")

        logger.info(
            "Новости для %s обработаны AI. Потрачено токенов: %d, примерная цена: $%.8f",
            country_code, ai_tokens, ai_price, extra=log_ctx
        )
    
    except Exception as e:
        logger.error(f"Ошибка при обработке страны={country_code}. Ошибка: {type(e)} - {str(e)}", extra=log_ctx, exc_info=True)
    
    return country_code, filtered_report, ai_tokens, ai_price, fetch_failures, fetch_alerts

def main_cycle() -> str:
    """
    Основная функция, возвращающая текст репорта
    """
    logger.info("-"*80)
    logger.info("--- Начало нового цикла обработки ---")

    try:
        feed_sources = load_feed_sources()
        all_source_urls = {url for urls in feed_sources.values() for url in urls}
    except Exception as e:
        logger.error(f"Ошибка при загрузке RSS лент: {type(e)} - {str(e)}", exc_info=True)
        return f"<b>Ошибка конфигурации:</b>\n<code>{escape_html(e)}</code>"

    if not feed_sources:
        logger.error("Источники новостей не настроены. Цикл обработки завершен.")
        return "⚠️ <b>Источники новостей (RSS) не настроены.</b>\nДобавьте их в <code>config.xlsx</code>."

    try:
        filter_client = OpenRouterClient(api_key=OPENROUTER_API_KEY, model=FILTER_MODEL)
        final_report_client = OpenRouterClient(api_key=OPENROUTER_API_KEY, model=FINAL_REPORT_MODEL)
        news_fetcher = NewsFetcher()
        
        logger.info("Синхронизация лент из источника с базой данных...")
        news_fetcher.sync_feeds_from_source(all_source_urls, feed_sources)
        
    except Exception as e:
        logger.error(f"Ошибка при инициализации клиентов: {type(e)} - {str(e)}", exc_info=True)
        return f"<b>Ошибка инициализации клиентов:</b>\n<code>{escape_html(e)}</code>"

    # Новая логика определения временного окна
    end_dt_utc = datetime.now(timezone.utc)
    last_run_time = news_fetcher.get_last_run_time()
    
    # Вычисляем время с последнего запуска
    time_since_last_run = end_dt_utc - last_run_time
    hours_since_last_run = time_since_last_run.total_seconds() / 3600
    
    # Ограничиваем максимальным временем парсинга
    if hours_since_last_run > MAX_PARSE_HOURS:
        start_dt_utc = end_dt_utc - timedelta(hours=MAX_PARSE_HOURS)
        logger.info("Время с последнего запуска (%.1f ч) превышает максимум (%d ч). Ограничиваем окно поиска.", 
                   hours_since_last_run, MAX_PARSE_HOURS)
    else:
        start_dt_utc = last_run_time
        logger.info("Парсим с времени последнего запуска (%.1f ч назад).", hours_since_last_run)
    
    logger.info("Временное окно для поиска: с %s по %s.", start_dt_utc.isoformat(), end_dt_utc.isoformat())

    filtered_reports: Dict[str, str] = {}
    total_ai_tokens: int = 0
    total_ai_price: float = 0.0
    all_failures: Dict[str, List[str]] = {}
    all_alerts: List[str] = []

    num_countries = len(feed_sources)
    workers = min(num_countries, MAX_CONCURRENT_WORKERS) if num_countries > 0 else 1
    
    # Цикл обработки каждой страны
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_country = {
            executor.submit(process_country, country, rss_links, news_fetcher, filter_client, start_dt_utc, end_dt_utc): country
            for country, rss_links in feed_sources.items()
        }
        for future in as_completed(future_to_country):
            country = future_to_country[future]
            try:
                _country, report, tokens, price, failures, alerts = future.result()
                if report: filtered_reports[_country] = report
                total_ai_tokens += tokens
                total_ai_price += price
                if failures: all_failures[_country] = failures
                if alerts: all_alerts.extend(alerts)
            except Exception as exc:
                logger.error("Ошибка при получении результата для страны '%s': %s", country, exc, exc_info=True)

    sorted_reports_list = sorted(filtered_reports.items())
    final_report_text = "\n\n".join(
        [f"Новости для: {country}:\n{report}" for country, report in sorted_reports_list if report.strip()]
    )

    # Финальный дайджест
    final_digest = ""
    if final_report_text:
        logger.info("Отправка для создания итогового дайджеста в AI...")
        try:
            result = final_report_client.create_chat_completion(
                system_prompt=FINAL_REPORT_SYSTEM_PROMPT,
                user_content=final_report_text
            )
            final_tokens = result.get('tokens', 0)
            final_price = result.get('price', 0.0)
            final_digest = result.get('result', "")
            
            total_ai_tokens += final_tokens
            total_ai_price += final_price
            
            logger.info("Итоговый дайджест успешно создан. Потрачено токенов: %d, цена: $%8f", final_tokens, final_price)
        except Exception as e:
            logger.error(f"Ошибка при создании итогового дайджеста через AI: {type(e)} - {str(e)}", exc_info=True)
            final_digest = f"⚠️ <b>Не удалось сгенерировать финальный дайджест:</b> <code>{escape_html(e)}</code>"

    # ИТОГОВАЯ СТАТИСТИКА ПОТРЕБЛЕНИЯ
    ai_stats = f"AI Total: {total_ai_tokens} токенов, ~${total_ai_price:.8f}"
    logger.info("Итоговая статистика цикла: %s", ai_stats)

    # СОХРАНЕНИЕ ИНФОРМАЦИИ О СБОЯХ В EXCEL
    if all_failures or all_alerts:
        logger.info("Сохранение информации о сбоях RSS в Excel...")
        try:
            update_excel_with_failures(all_failures, all_alerts)
        except Exception as e:
            logger.error("Ошибка при сохранении сбоев в Excel: %s", e, exc_info=True)

    # Обновляем время последнего запуска в базе
    try:
        news_fetcher.update_last_run_time(end_dt_utc)
    except Exception as e:
        logger.error("Ошибка при обновлении времени последнего запуска: %s", e)

    logger.info("--- Цикл обработки завершен ---")
    logger.info("-"*80)
    
    return final_digest
