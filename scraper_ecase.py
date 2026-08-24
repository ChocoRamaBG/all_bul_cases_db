import os
import csv
import json
import re
import time
from datetime import datetime
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ============================================================
# eCase FULL CASE SCRAPER (Autonomous GitHub Actions Mode)
# ============================================================

START_TIME = time.time()
TIME_LIMIT_SECONDS = 5.4 * 60 * 60  # ~5 часа и 24 минути

MASTER_URL = "https://ecase.justice.bg/Case"
BASE_URL = "https://ecase.justice.bg"
MASTER_PAGE_SIZE = "50"
DELAY_BETWEEN_CASES_MS = 3500  # УВЕЛИЧЕНО: 3.5 секунди за да не ядеш банана
DELAY_AFTER_AJAX_MS = 500      # УВЕЛИЧЕНО: 0.5 секунди за стабилност

# ============================================================
# ПЪТИЩА И ДИРЕКТОРИИ
# ============================================================
try:
    output_dir = os.path.dirname(os.path.abspath(__file__))
except NameError:
    output_dir = os.getcwd()

OUTPUT_DIR = os.path.join(output_dir, "ecase_outputs")
os.makedirs(OUTPUT_DIR, exist_ok=True)

MEMORY_FILE = os.path.join(OUTPUT_DIR, "memory.json")
ERROR_FILE = os.path.join(OUTPUT_DIR, "errors.jsonl")
STATE_FILE = os.path.join(OUTPUT_DIR, "savegame_ecase.json")
CONTINUE_FLAG_FILE = os.path.join(OUTPUT_DIR, "CONTINUE_FLAG_ECASE")

FILES = {
    "cases": os.path.join(OUTPUT_DIR, "cases.csv"),
    "parties": os.path.join(OUTPUT_DIR, "case_parties.csv"),
    "assignments": os.path.join(OUTPUT_DIR, "case_assignments.csv"),
    "hearings": os.path.join(OUTPUT_DIR, "case_hearings.csv"),
    "acts": os.path.join(OUTPUT_DIR, "case_acts.csv"),
    "connected": os.path.join(OUTPUT_DIR, "case_connected_cases.csv"),
    "chronology": os.path.join(OUTPUT_DIR, "case_chronology.csv"),
}

CSV_HEADERS = {
    "cases": [
        "Case_GID", "Case_Number", "Year", "Case_Type", "Court",
        "Claimant", "Defendant", "Reporting_Judge", "Department",
        "Judicial_Panel", "Formation_Date", "Incoming_Number",
        "Case_URL", "Scraped_At",
    ],
    "parties": [
        "Case_GID", "Case_Number", "Year", "Case_Type", "Court",
        "Party_Name", "Quality", "Lawyers", "Procedural_Quality",
    ],
    "assignments": [
        "Case_GID", "Assignment_Date", "Judge_or_Assessor",
        "Type", "Assigned_By", "Details_GID",
    ],
    "hearings": [
        "Case_GID", "Hearing_Date", "Hearing_Type", "Result",
        "Secretary", "Prosecutor", "Details_GID",
    ],
    "acts": [
        "Case_GID", "Act_Date", "Act_Type", "Act_Number",
        "Effective_From", "Judicial_Panel", "Details_GID",
    ],
    "connected": [
        "Case_GID", "Court", "Case_Type", "Case_Number",
        "Year", "Connected_Case_GID",
    ],
    "chronology": [
        "Case_GID", "Chronology_Text",
    ],
}

UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

# ============================================================
# УПРАВЛЕНИЕ НА ВРЕМЕТО И СЪСТОЯНИЕТО (STATE)
# ============================================================
def time_limit_reached():
    return (time.time() - START_TIME) >= TIME_LIMIT_SECONDS

def flag_for_continuation():
    with open(CONTINUE_FLAG_FILE, 'w') as f:
        f.write("CONTINUE")

def clear_continuation_flag():
    if os.path.exists(CONTINUE_FLAG_FILE):
        os.remove(CONTINUE_FLAG_FILE)

state = {
    "current_page": 1
}

if os.path.exists(STATE_FILE):
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state.update(json.load(f))
        print(f"[INFO] Възстановяване на сесията от страница {state['current_page']}.")
    except Exception as e:
        print(f"[WARN] Грешка при зареждане на състоянието: {e}")

def save_state():
    temp_file = STATE_FILE + ".tmp"
    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(temp_file, STATE_FILE)
    except Exception as e:
        print(f"[ERROR] Неуспешен запис на state файл: {e}")

# ============================================================
# ПОМОЩНИ ФУНКЦИИ ЗА ДАННИ И ФАЙЛОВЕ
# ============================================================
def clean(text):
    if text is None:
        return ""
    return " ".join(str(text).split()).strip()

def gid_from(text):
    if not text:
        return None
    m = UUID_RE.search(text)
    return m.group(0) if m else None

def load_memory():
    if not os.path.exists(MEMORY_FILE):
        return set()
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data) if isinstance(data, list) else set()
    except Exception:
        print("[WARNING] memory.json is unreadable. Starting with empty memory.")
        return set()

def save_memory(memory):
    temp = MEMORY_FILE + ".tmp"
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(sorted(memory), f, ensure_ascii=False, indent=2)
    os.replace(temp, MEMORY_FILE)

def log_error(gid, stage, error):
    record = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "gid": gid,
        "stage": stage,
        "error": str(error),
    }
    with open(ERROR_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

def ensure_csv(kind):
    path = FILES[kind]
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerow(CSV_HEADERS[kind])

def append_rows(kind, rows):
    if not rows:
        return
    ensure_csv(kind)
    with open(FILES[kind], "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerows(rows)
        f.flush()
        os.fsync(f.fileno())

def visible_output(item, label):
    loc = item.locator(f".list__label:has-text('{label}') + .list__output").first
    if loc.count() > 0:
        try:
            return clean(loc.text_content())
        except Exception:
            return ""
    return ""

def page_location(locator):
    if locator.count() > 0:
        try:
            return clean(locator.text_content())
        except Exception:
            return ""
    return ""

def parse_range_from_location(text):
    m = re.search(r"(\d+)\s*-\s*(\d+)\s+от\s+(\d+)", text)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))

# ============================================================
# ЛОГИКА ЗА НАВИГАЦИЯ
# ============================================================
def set_master_page_size(page):
    try:
        select = page.locator("#gvMain select[aria-label='Филтрирай по']")
        if select.count() == 0:
            return
        current = select.input_value()
        if current == MASTER_PAGE_SIZE:
            return
        with page.expect_response(
            lambda r: "/Case/LoadData" in r.url and r.status == 200,
            timeout=30000,
        ):
            select.select_option(MASTER_PAGE_SIZE)
        page.wait_for_timeout(DELAY_AFTER_AJAX_MS)
        page.wait_for_selector("#gvMain .list__item a.case-card", timeout=20000)
    except PlaywrightTimeoutError:
        pass
    except Exception as e:
        print(f"[WARNING] Could not set master page size to {MASTER_PAGE_SIZE}: {e}")

def master_total_pages(page):
    location = page_location(page.locator("#gvMain .page-location").first)
    parsed = parse_range_from_location(location)
    if not parsed:
        return 1
    first, last, total = parsed
    page_size = max(1, last - first + 1)
    return (total + page_size - 1) // page_size

def master_next(page):
    btn = page.locator("#gvMain li.page-next:not(.page-inactive) a.page-link")
    if btn.count() == 0:
        return False
    try:
        with page.expect_response(
            lambda r: "/Case/LoadData" in r.url and r.status == 200,
            timeout=30000,
        ):
            btn.click()
        page.wait_for_timeout(DELAY_AFTER_AJAX_MS)
        page.wait_for_selector("#gvMain .list__item a.case-card", timeout=20000)
        return True
    except Exception as e:
        print(f"[ERROR] Master pagination failed: {e}")
        return False

# ============================================================
# ЕКСТРАКЦИЯ НА ДАННИ (МЕТАДАННИ, СТРАНИ И Т.Н.)
# ============================================================
def case_metadata(page, gid):
    data = {
        "Case_GID": gid, "Case_Number": "", "Year": "", "Case_Type": "",
        "Court": "", "Claimant": "", "Defendant": "", "Reporting_Judge": "",
        "Department": "", "Judicial_Panel": "", "Formation_Date": "",
        "Incoming_Number": "", "Case_URL": page.url,
        "Scraped_At": datetime.now().isoformat(timespec="seconds"),
    }
    title_loc = page.locator("h1.page-title")
    if title_loc.count() > 0:
        try:
            title = clean(title_loc.text_content())
            m = re.search(r"Дело\s*№\s*(\S+)\s+(\d{4})", title)
            if m:
                data["Case_Number"] = m.group(1)
                data["Year"] = m.group(2)
        except: pass

    type_loc = page.locator(".heading__subheading h2")
    if type_loc.count() > 0:
        try: data["Case_Type"] = clean(type_loc.text_content())
        except: pass

    try:
        cells = page.locator(".case__assets .list__cell").all()
        for cell in cells:
            label_loc = cell.locator(".list__label")
            output_loc = cell.locator(".list__output")
            if label_loc.count() > 0 and output_loc.count() > 0:
                label = clean(label_loc.text_content())
                value = clean(output_loc.text_content())
                if label == "Инициираща страна": data["Claimant"] = value
                elif label == "Ответна страна": data["Defendant"] = value
                elif label == "Съд": data["Court"] = value
                elif label == "Съдия докладчик": data["Reporting_Judge"] = value
                elif label == "Дата на образуване": data["Formation_Date"] = value
                elif label == "Входящ номер": data["Incoming_Number"] = value
                elif label == "Съдебен състав": data["Judicial_Panel"] = value
    except: pass

    if not data["Judicial_Panel"]:
        data["Judicial_Panel"] = ""
    return data

def extract_master_card(anchor):
    href = anchor.get_attribute("href") or ""
    gid = gid_from(href)
    if not gid: return None

    card = {
        "gid": gid,
        "url": href if href.startswith("http") else BASE_URL + href,
        "case_number": "", "year": "", "case_type": "", "court": "",
        "claimant": "", "defendant": "", "judge": "",
        "department": "", "panel": "",
    }

    header1_loc = anchor.locator(".case-card__header h3").first
    if header1_loc.count() > 0:
        try:
            heading = clean(header1_loc.text_content())
            m = re.search(r"№\s*(\d+)\s+от\s+(\d{4})", heading)
            if m:
                card["case_number"] = m.group(1)
                card["year"] = m.group(2)
        except: pass

    header2_loc = anchor.locator(".case-card__header h3").nth(1)
    if header2_loc.count() > 0:
        try: card["case_type"] = clean(header2_loc.text_content())
        except: pass

    for cell in anchor.locator(".case-card__body .col-md").all():
        label_loc = cell.locator(".list__label")
        output_loc = cell.locator(".list__output")
        if label_loc.count() == 0 or output_loc.count() == 0: continue
        try:
            label = clean(label_loc.text_content())
            value = clean(output_loc.text_content())
        except: continue
        
        if label == "Съд": card["court"] = value
        elif label == "Ищец": card["claimant"] = value
        elif label == "Ответник": card["defendant"] = value
        elif label == "Съдия докладчик": card["judge"] = value
        elif label == "Отделение": card["department"] = value
        card["panel"] = value

    return card

def ensure_case_loaded(page):
    try:
        # ПРАВИЛНИЯТ НАЧИН: чакаме макс 8 секунди, ако го няма - не крашваме, гащник!
        page.wait_for_selector("#caseTabSides", state="attached", timeout=8000)
        page.wait_for_selector("#gvSides .list__item", state="visible", timeout=5000)
    except PlaywrightTimeoutError:
        print("[WARN] Секцията със страните не зареди (възможно е скрито дело). Продължаваме напред.")
        pass

def click_case_from_master(master, context, anchor):
    try:
        with context.expect_page(timeout=15000) as page_info:
            anchor.click(modifiers=["Control"])
        return page_info.value
    except PlaywrightTimeoutError:
        href = anchor.get_attribute("href")
        if not href: raise
        page = context.new_page()
        page.goto(BASE_URL + href if href.startswith("/") else href, wait_until="domcontentloaded")
        return page

def read_sides(page, meta):
    rows = []
    while True:
        for item in page.locator("#gvSides .list__item").all():
            rows.append([
                meta["Case_GID"], meta["Case_Number"], meta["Year"],
                meta["Case_Type"], meta["Court"],
                visible_output(item, "Име"),
                visible_output(item, "Качество"),
                visible_output(item, "Адвокати"),
                visible_output(item, "Процесуално качество"),
            ])
        btn = page.locator("#gvSides li.page-next:not(.page-inactive) a.page-link")
        if btn.count() == 0: break
        try:
            with page.expect_response(
                lambda r: "/Case/SidesLoadData" in r.url and r.status == 200, timeout=15000,
            ): btn.click()
            page.wait_for_timeout(DELAY_AFTER_AJAX_MS)
            page.wait_for_selector("#gvSides .list__item", state="visible", timeout=15000)
        except Exception as e:
            # Ако не успее пагинацията, по-добре да върнем каквото сме събрали до момента
            print(f"[WARN] Sides pagination failed: {e}")
            break
    return rows

def click_tab(page, tab_text, panel_selector):
    btn = page.get_by_role("button", name=tab_text).first
    if btn.count() == 0: return False
    try:
        endpoint = {
            "Разпределения": "/Case/AssignmentsLoadData",
            "Заседания": "/Case/HearingsLoadData",
            "Актове": "/Case/ActsLoadData",
        }.get(tab_text)
        if endpoint:
            try:
                with page.expect_response(
                    lambda r: endpoint in r.url and r.status == 200, timeout=15000,
                ): btn.click()
            except PlaywrightTimeoutError: btn.click()
        else: btn.click()
        page.wait_for_timeout(DELAY_AFTER_AJAX_MS)
        page.wait_for_selector(panel_selector, state="attached", timeout=10000)
        return True
    except: return False

def read_assignments(page, gid):
    if not click_tab(page, "Разпределения", "#gvAssignments"): return []
    rows = []
    for item in page.locator("#gvAssignments .list__item").all():
        text_content = item.text_content() or ""
        rows.append([
            gid, visible_output(item, "Дата"), visible_output(item, "Съдия/заседател"),
            visible_output(item, "Тип"), visible_output(item, "Разпределил"), gid_from(text_content) or "",
        ])
    return rows

def read_hearings(page, gid):
    if not click_tab(page, "Заседания", "#gvHearings"): return []
    rows = []
    for item in page.locator("#gvHearings .list__item").all():
        text_content = item.text_content() or ""
        rows.append([
            gid, visible_output(item, "Начало"), visible_output(item, "Вид"),
            visible_output(item, "Резултат"), visible_output(item, "Секретар"),
            visible_output(item, "Прокурор"), gid_from(text_content) or "",
        ])
    return rows

def read_acts(page, gid):
    if not click_tab(page, "Актове", "#gvActs"): return []
    rows = []
    for item in page.locator("#gvActs .list__item").all():
        title_loc = item.locator(".list__title").first
        title_text = clean(title_loc.text_content()) if title_loc.count() > 0 else ""
        text_content = item.text_content() or ""
        rows.append([
            gid, title_text, visible_output(item, "Вид"), visible_output(item, "Номер"),
            visible_output(item, "В сила от"), visible_output(item, "Съдебен състав"), gid_from(text_content) or "",
        ])
    return rows

def read_connected(page, gid):
    if not page.locator("#caseConnectedCase").count(): return []
    btn = page.locator("button[data-bs-target='#caseConnectedCase']")
    if btn.count() == 0: return []
    try:
        with page.expect_response(
            lambda r: "/Case/ConnectedCaseLoadData" in r.url and r.status == 200, timeout=15000,
        ): btn.click()
    except PlaywrightTimeoutError: btn.click()
    page.wait_for_timeout(DELAY_AFTER_AJAX_MS)
    rows = []
    for item in page.locator("#gvConnectedCase .list__item").all():
        text_content = item.text_content() or ""
        rows.append([
            gid, visible_output(item, "Съд"), visible_output(item, "Вид"),
            visible_output(item, "Номер"), visible_output(item, "Година"), gid_from(text_content) or "",
        ])
    return rows

def read_chronology(page, gid):
    if not page.locator("button[onclick*='loadChronology']").count(): return []
    try:
        with page.expect_response(
            lambda r: "/Case/ChronologyTimelineByCase" in r.url and r.status == 200, timeout=15000,
        ): page.locator("button[onclick*='loadChronology']").click()
    except PlaywrightTimeoutError: page.locator("button[onclick*='loadChronology']").click()
    page.wait_for_timeout(DELAY_AFTER_AJAX_MS)
    chron_loc = page.locator("#gvChronology")
    text = clean(chron_loc.text_content()) if chron_loc.count() > 0 else ""
    return [[gid, text]] if text else []

def persist_case(master_card, meta, parties, assignments, hearings, acts, connected, chronology):
    def pick(detail_key, master_key):
        return meta.get(detail_key) or master_card.get(master_key) or ""

    case_row = [[
        meta["Case_GID"], pick("Case_Number", "case_number"), pick("Year", "year"),
        pick("Case_Type", "case_type"), pick("Court", "court"), pick("Claimant", "claimant"),
        pick("Defendant", "defendant"), pick("Reporting_Judge", "judge"),
        meta.get("Department") or "", meta.get("Judicial_Panel") or master_card.get("panel") or "",
        meta.get("Formation_Date") or "", meta.get("Incoming_Number") or "",
        meta.get("Case_URL") or master_card.get("url") or "", meta.get("Scraped_At") or "",
    ]]

    normalized_parties = []
    for row in parties:
        row[1] = pick("Case_Number", "case_number")
        row[2] = pick("Year", "year")
        row[3] = pick("Case_Type", "case_type")
        row[4] = pick("Court", "court")
        normalized_parties.append(row)

    append_rows("cases", case_row)
    append_rows("parties", normalized_parties)
    append_rows("assignments", assignments)
    append_rows("hearings", hearings)
    append_rows("acts", acts)
    append_rows("connected", connected)
    append_rows("chronology", chronology)

def process_case(master_card, master, context, memory):
    gid = master_card["gid"]
    if gid in memory: return True

    print(f"\n[CASE] {master_card['case_number']}/{master_card['year']} | {gid}")
    case_page = None
    try:
        anchor = master.locator(f"#gvMain a.case-card[href*='{gid}']").first
        if anchor.count() == 0: raise RuntimeError("Case link disappeared from current master page")
        case_page = click_case_from_master(master, context, anchor)
        case_page.wait_for_load_state("domcontentloaded", timeout=25000)
        
        ensure_case_loaded(case_page)
        
        # ЗАЩИТА: Проверяваме дали не сме изяли банана
        page_text = case_page.content().lower()
        if "cloudflare" in page_text or "access denied" in page_text or "rate limit" in page_text:
            raise RuntimeError("Детектиран IP Ban / Защита. Спираме обработката на това дело.")

        meta = case_metadata(case_page, gid)
        parties = read_sides(case_page, meta)
        assignments = read_assignments(case_page, gid)
        hearings = read_hearings(case_page, gid)
        acts = read_acts(case_page, gid)
        connected = read_connected(case_page, gid)
        chronology = read_chronology(case_page, gid)

        persist_case(master_card, meta, parties, assignments, hearings, acts, connected, chronology)
        
        memory.add(gid)
        save_memory(memory)

        print(
            f"[SUCCESS] {gid} | parties={len(parties)} | "
            f"assignments={len(assignments)} | hearings={len(hearings)} | "
            f"acts={len(acts)} | connected={len(connected)}"
        )
        return True
    except Exception as e:
        print(f"[ERROR] {gid}: {e}")
        log_error(gid, "process_case", e)
        return False
    finally:
        if case_page is not None:
            try: case_page.close()
            except Exception: pass
        master.wait_for_timeout(DELAY_BETWEEN_CASES_MS)

# ============================================================
# MAIN
# ============================================================
def main():
    clear_continuation_flag()

    for kind in FILES:
        ensure_csv(kind)

    memory = load_memory()

    print("=" * 78)
    print("eCase AUTONOMOUS FULL SCRAPER - STABLE SPEED")
    print("=" * 78)
    print(f"Output directory: {OUTPUT_DIR}")
    print(f"Already completed: {len(memory)} cases")
    print(f"Master page size: {MASTER_PAGE_SIZE}")
    print("=" * 78)

    with sync_playwright() as p:
        # headless=True за сървърна среда (GitHub Actions)
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        )
        context = browser.new_context(viewport={"width": 1920, "height": 1080})
        
        # Блокиране на излишни мрежови ресурси за по-бързо рендиране
        context.route("**/*", lambda route: route.continue_() if route.request.resource_type not in ["image", "font", "media"] else route.abort())
        
        master = context.new_page()

        try:
            master.goto(MASTER_URL, wait_until="domcontentloaded", timeout=30000)
            master.wait_for_selector("#gvMain .list__item a.case-card", state="visible", timeout=20000)

            set_master_page_size(master)
            total_pages = master_total_pages(master)
            print(f"[MASTER] Estimated total pages: {total_pages:,}")

            current_page = state["current_page"]

            # Бързо превъртане (fast-forward) до запазената страница
            if current_page > 1:
                print(f"[INFO] Fast-forwarding pagination to page {current_page}...")
                actual_page = 1
                while actual_page < current_page:
                    if not master_next(master):
                        print("[ERROR] Failed to fast-forward. Breaking pagination catch-up.")
                        break
                    actual_page += 1
                    if actual_page % 10 == 0:
                        print(f"  -> Reached page {actual_page}")

            while current_page <= total_pages:
                if time_limit_reached():
                    print("\n[INFO] Лимитът на времето е достигнат. Флагът за продължение е активиран.")
                    flag_for_continuation()
                    break

                master.wait_for_selector("#gvMain .list__item a.case-card", state="visible", timeout=20000)
                anchors = master.locator("#gvMain .list__item a.case-card")
                count = anchors.count()
                print(f"\n[MASTER] Page {current_page:,}/{total_pages:,} | {count} cases")

                cards = []
                for i in range(count):
                    card = extract_master_card(anchors.nth(i))
                    if card: cards.append(card)

                time_limit_hit_in_profiles = False
                for index, card in enumerate(cards, 1):
                    if time_limit_reached():
                        print("[INFO] Лимитът на времето е достигнат по време на обхождане на профили.")
                        flag_for_continuation()
                        time_limit_hit_in_profiles = True
                        break

                    if card["gid"] in memory:
                        print(f"[SKIP] {index}/{len(cards)} already done: {card['gid']}")
                        continue
                        
                    print(f"[QUEUE] {index}/{len(cards)}")
                    process_case(card, master, context, memory)

                if time_limit_hit_in_profiles:
                    break

                if current_page >= total_pages:
                    break
                    
                if not master_next(master):
                    print("[STOP] Could not advance master pagination.")
                    break
                    
                current_page += 1
                state["current_page"] = current_page
                save_state()

            print("\n" + "=" * 78)
            print("Automatic crawl cycle finished.")
            print("=" * 78)

        finally:
            browser.close()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[INFO] Прекъснато от потребител.")
