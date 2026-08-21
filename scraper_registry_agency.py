import json
import os
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


# GitHub Actions usually gives a job up to 6 hours.
# Leave a safety margin so state can be saved and the workflow can exit cleanly.
START_TIME = time.time()
TIME_LIMIT_SECONDS = 5.4 * 60 * 60

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "registry_agency_outputs"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

OUTPUT_FILE = OUTPUT_DIR / "100_percent_valid_uics.txt"
PROCESSED_QUERIES_FILE = OUTPUT_DIR / "processed_queries.txt"
STATE_FILE = OUTPUT_DIR / "savegame_registry_agency.json"
CONTINUE_FLAG_FILE = OUTPUT_DIR / "CONTINUE_FLAG_REGISTRY_AGENCY"

SEARCH_URL = (
    "https://portal.registryagency.bg/CR/Reports/"
    "VerificationPersonOrg?name={query}&selectedSearchFilter=1"
)

RATE_LIMIT_TEXT = "Достигнат е максимално допустимият брой заявки"

HEADLESS = os.getenv("HEADLESS", "true").lower() not in {"0", "false", "no"}
RATE_LIMIT_SLEEP_SECONDS = int(os.getenv("RATE_LIMIT_SLEEP_SECONDS", "300"))
NAVIGATION_TIMEOUT_MS = int(os.getenv("NAVIGATION_TIMEOUT_MS", "30000"))
SELECTOR_TIMEOUT_MS = int(os.getenv("SELECTOR_TIMEOUT_MS", "7000"))
PAGE_CHANGE_DELAY_MS = int(os.getenv("PAGE_CHANGE_DELAY_MS", "1000"))


def log_msg(message: str) -> None:
    current_time = datetime.now().strftime("%H:%M:%S")
    print(f"[{current_time}] {message}", flush=True)


def time_limit_reached() -> bool:
    return (time.time() - START_TIME) >= TIME_LIMIT_SECONDS


def flag_for_continuation() -> None:
    try:
        CONTINUE_FLAG_FILE.write_text("CONTINUE\n", encoding="utf-8")
    except OSError as exc:
        log_msg(f"[WARN] Не успях да запиша continuation flag: {exc}")


def clear_continuation_flag() -> None:
    try:
        CONTINUE_FLAG_FILE.unlink(missing_ok=True)
    except OSError as exc:
        log_msg(f"[WARN] Не успях да изтрия continuation flag: {exc}")


def atomic_write_json(path: Path, payload: dict) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)


def load_state() -> dict:
    state = {
        "query_index": 0,
        "current_query": None,
        "page_number": 1,
        "saved_at": None,
    }

    if not STATE_FILE.exists():
        return state

    try:
        with STATE_FILE.open("r", encoding="utf-8") as handle:
            saved = json.load(handle)
        if isinstance(saved, dict):
            state["query_index"] = int(saved.get("query_index", 0))
            state["current_query"] = saved.get("current_query")
            state["page_number"] = max(1, int(saved.get("page_number", 1)))
            state["saved_at"] = saved.get("saved_at")
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        log_msg(f"[WARN] Повреден/нечетим state файл, започваме от записаната memory: {exc}")

    return state


def save_state(
    state: dict,
    query_index: int,
    current_query: str | None,
    page_number: int,
) -> None:
    payload = {
        "query_index": query_index,
        "current_query": current_query,
        "page_number": page_number,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
    }

    try:
        atomic_write_json(STATE_FILE, payload)
        state.clear()
        state.update(payload)
    except OSError as exc:
        log_msg(f"[WARN] Не успях да запиша state: {exc}")


def load_set(path: Path) -> set[str]:
    values: set[str] = set()

    if not path.exists():
        return values

    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                value = line.strip()
                if value:
                    values.add(value)
    except OSError as exc:
        log_msg(f"[WARN] Не успях да прочета {path}: {exc}")

    return values


def append_line(path: Path, value: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{value}\n")


def build_search_queries() -> list[str]:
    # Bulgarian Cyrillic А-Я
    bg_alphabet = [chr(codepoint) for codepoint in range(1040, 1072)]
    # Latin A-Z
    en_alphabet = [chr(codepoint) for codepoint in range(65, 91)]
    # Digits 0-9
    digits = [str(number) for number in range(10)]

    all_chars = bg_alphabet + en_alphabet + digits
    single_chars = all_chars
    double_chars = [a + b for a in all_chars for b in all_chars]

    return single_chars + double_chars


def is_rate_limited(page) -> bool:
    try:
        return RATE_LIMIT_TEXT in page.content()
    except Exception:
        return False


def create_browser(p):
    browser = p.chromium.launch(
        headless=HEADLESS,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
        ],
    )
    context = browser.new_context(
        locale="bg-BG",
        extra_http_headers={"Accept-Language": "bg-BG,bg;q=0.9"},
        viewport={"width": 1280, "height": 720},
    )
    page = context.new_page()
    page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)
    page.set_default_timeout(SELECTOR_TIMEOUT_MS)
    return browser, context, page


def open_query(page, query: str) -> None:
    encoded_query = urllib.parse.quote(query, safe="")
    url = SEARCH_URL.format(query=encoded_query)
    page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)

    if is_rate_limited(page):
        raise RuntimeError("RATE_LIMIT")

    # The site can legitimately return no rows. We only wait briefly for the
    # table to attach; the caller decides whether that means "empty query".
    try:
        page.wait_for_selector(
            "table.table-collapsible tbody",
            state="attached",
            timeout=SELECTOR_TIMEOUT_MS,
        )
    except PlaywrightTimeoutError:
        if is_rate_limited(page):
            raise RuntimeError("RATE_LIMIT")


def extract_current_page_uics(page, extracted_uics: set[str], query: str, page_number: int) -> int:
    if is_rate_limited(page):
        raise RuntimeError("RATE_LIMIT")

    rows = page.locator(
        "table.table-collapsible tbody tr:not(.collapsible-row)"
    ).all()

    new_uics = 0

    for row in rows:
        columns = row.locator("td").all()

        if len(columns) < 3:
            continue

        uic_text = columns[2].locator("p.field-text").inner_text().strip()
        uic_clean = "".join(character for character in uic_text if character.isdigit())

        if len(uic_clean) >= 9 and uic_clean not in extracted_uics:
            append_line(OUTPUT_FILE, uic_clean)
            extracted_uics.add(uic_clean)
            new_uics += 1

    log_msg(
        f"[{query} - Стр {page_number}] Извлечени. "
        f"Нови: {new_uics}. Общо в базата: {len(extracted_uics)}"
    )
    return new_uics


def move_to_next_page(page) -> bool:
    next_button = page.locator("li.page-item.next:not(.disabled) a").first

    try:
        if next_button.count() == 0 or not next_button.is_visible():
            return False

        next_button.click(timeout=SELECTOR_TIMEOUT_MS)
        page.wait_for_timeout(PAGE_CHANGE_DELAY_MS)

        if is_rate_limited(page):
            raise RuntimeError("RATE_LIMIT")

        return True

    except PlaywrightTimeoutError:
        if is_rate_limited(page):
            raise RuntimeError("RATE_LIMIT")
        return False


def main() -> int:
    clear_continuation_flag()

    extracted_uics = load_set(OUTPUT_FILE)
    processed_queries = load_set(PROCESSED_QUERIES_FILE)
    search_queries = build_search_queries()
    state = load_state()

    query_index = max(0, min(state["query_index"], len(search_queries)))
    log_msg(f"[СТАРТ] Заредени ЕИК номера: {len(extracted_uics)}")
    log_msg(f"[СТАРТ] Завършени търсения: {len(processed_queries)}")
    log_msg(f"[СТАРТ] Общо комбинации: {len(search_queries)}")
    log_msg(
        f"[СТАРТ] Продължаваме от индекс {query_index}, "
        f"query={state.get('current_query')!r}, page={state.get('page_number', 1)}"
    )

    with sync_playwright() as p:
        browser = None

        try:
            browser, context, page = create_browser(p)

            while query_index < len(search_queries):
                if time_limit_reached():
                    log_msg("[ВРЕМЕТО ИЗТЕЧЕ] Запазваме прогрес и приключваме текущия job.")
                    flag_for_continuation()
                    save_state(state, query_index, search_queries[query_index], 1)
                    return 0

                query = search_queries[query_index]

                if query in processed_queries:
                    query_index += 1
                    continue

                page_number = (
                    state["page_number"]
                    if state.get("current_query") == query
                    else 1
                )

                try:
                    # Always start the query from page 1. If state says a later page,
                    # replay pagination until that page. Duplicate UICs are harmless
                    # because extracted_uics is a set.
                    open_query(page, query)

                    if page_number > 1:
                        for _ in range(1, page_number):
                            if time_limit_reached():
                                flag_for_continuation()
                                save_state(state, query_index, query, page_number)
                                return 0

                            if not move_to_next_page(page):
                                raise RuntimeError(
                                    f"Не успяхме да възстановим страница {page_number} "
                                    f"за query '{query}'."
                                )

                    while True:
                        if time_limit_reached():
                            log_msg(
                                f"[ВРЕМЕТО ИЗТЕЧЕ] Query '{query}', "
                                f"страница {page_number}. Запазваме state."
                            )
                            flag_for_continuation()
                            save_state(state, query_index, query, page_number)
                            return 0

                        extract_current_page_uics(
                            page,
                            extracted_uics,
                            query,
                            page_number,
                        )

                        # Save after every scraped page. If the job dies, the page
                        # can be replayed safely because UIC writes are deduplicated.
                        save_state(state, query_index, query, page_number)

                        if move_to_next_page(page):
                            page_number += 1
                            save_state(state, query_index, query, page_number)
                            continue

                        log_msg(f"[УСПЕХ] Комбинацията '{query}' е напълно източена.")
                        append_line(PROCESSED_QUERIES_FILE, query)
                        processed_queries.add(query)

                        query_index += 1
                        save_state(
                            state,
                            query_index,
                            search_queries[query_index] if query_index < len(search_queries) else None,
                            1,
                        )
                        break

                except RuntimeError as exc:
                    if "RATE_LIMIT" not in str(exc):
                        log_msg(f"[ГРЕШКА] при '{query}': {exc}")
                        # Do not mark the query complete. Retry it in the next loop.
                        # A small pause prevents tight retry loops on transient failures.
                        time.sleep(5)
                        continue

                    log_msg(
                        "[БЛОКАЖ] Registry Agency върна rate-limit. "
                        f"Заспиваме за {RATE_LIMIT_SLEEP_SECONDS} секунди и рестартираме browser-а."
                    )

                    save_state(state, query_index, query, page_number)

                    try:
                        browser.close()
                    except Exception:
                        pass

                    browser = None

                    if time_limit_reached() or (
                        time.time() - START_TIME + RATE_LIMIT_SLEEP_SECONDS
                        >= TIME_LIMIT_SECONDS
                    ):
                        log_msg("[ИНФО] Няма достатъчно оставащо време за rate-limit sleep.")
                        flag_for_continuation()
                        return 0

                    time.sleep(RATE_LIMIT_SLEEP_SECONDS)

                    log_msg(
                        f"[INFO] Събуждане. Повтаряме '{query}' "
                        f"от страница {page_number}."
                    )
                    browser, context, page = create_browser(p)

        finally:
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass

    if query_index >= len(search_queries):
        clear_continuation_flag()
        log_msg("[КРАЙ] Всички комбинации са напълно сканирани!")
    else:
        flag_for_continuation()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
