"""
Naukri Auto-Apply Bot - Recommended Jobs Edition
=================================================
Applies to recommended jobs on Naukri.com's dedicated page.
Selects up to 5 jobs, clicks Apply, then handles the chatbot
questionnaire drawer with a self-learning answer system.

Key features:
  - Radio buttons: auto-filled from SQLite database, or clicked by user in browser
  - Free-text questions: detected via contenteditable div, user types in browser
  - Self-learning: watches what you answer in the browser, saves to SQLite automatically
  - Next run: previously seen questions are auto-filled

Usage:
    1. Copy .env.example to .env and fill in your details
    2. pip install -r requirements.txt
    3. python autoapply.py

Page: https://www.naukri.com/mnjuser/recommendedjobs
"""

import difflib
import logging
import logging.handlers
import os
import random
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.edge.service import Service as EdgeService
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

try:
    from webdriver_manager.microsoft import EdgeChromiumDriverManager
    WEBDRIVER_MANAGER_AVAILABLE = True
except ImportError:
    WEBDRIVER_MANAGER_AVAILABLE = False

# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------
load_dotenv()

NAUKRI_EMAIL: str = os.getenv('NAUKRI_EMAIL', '')
NAUKRI_PASSWORD: str = os.getenv('NAUKRI_PASSWORD', '')

TABS: List[Dict[str, Any]] = [
    {"num": 1, "id": "apply",        "label": "Applies"},
    {"num": 2, "id": "profile",      "label": "Profile"},
    {"num": 3, "id": "top_candidate", "label": "Top Candidate"},
    {"num": 4, "id": "preference",   "label": "Preferences"},
    {"num": 5, "id": "similar_jobs", "label": "You might like"},
]

# ------------------------------------------------------------
# Logging Setup — Enterprise Standard (Console + Rotating File)
# ------------------------------------------------------------

def setup_logging(log_file: str = "naukri_bot.log", max_bytes: int = 5 * 1024 * 1024, backup_count: int = 3) -> logging.Logger:
    """
    Configure enterprise-grade dual-output logging.

    - Console: INFO level, concise format for terminal visibility.
    - File: DEBUG level, structured format with function/line info,
      using a rotating file handler to prevent unbounded growth.

    Args:
        log_file: Path to the log file.
        max_bytes: Maximum size per log file before rotation.
        backup_count: Number of backup files to retain.

    Returns:
        A configured logger instance.
    """
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.DEBUG)

    # Prevent duplicate handlers on re-import/reload
    if logger.handlers:
        logger.handlers.clear()

    # Console handler — concise, user-friendly
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_format = logging.Formatter(
        fmt='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%H:%M:%S'
    )
    console_handler.setFormatter(console_format)

    # Rotating file handler — structured, audit-grade
    file_handler = logging.handlers.RotatingFileHandler(
        filename=log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding='utf-8'
    )
    file_handler.setLevel(logging.DEBUG)
    file_format = logging.Formatter(
        fmt='%(asctime)s [%(levelname)s] %(funcName)s:%(lineno)d - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    file_handler.setFormatter(file_format)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    return logger


logger = setup_logging()


# ============================================================
# Constants — Human Behavior Simulation
# ============================================================
HUMAN_TYPING_DELAY: Tuple[float, float] = (0.04, 0.12)
HUMAN_CLICK_PAUSE: Tuple[float, float] = (0.3, 1.0)
HUMAN_NAVIGATION_PAUSE: Tuple[float, float] = (1.0, 3.5)

# ============================================================
# Constants — Screenshot Configuration
# ============================================================
SCREENSHOT_DIR: Path = Path("screenshots")
SCREENSHOT_DIR.mkdir(exist_ok=True)

# ============================================================
# Constants — Application
# ============================================================
ANSWERS_DB: str = "application_answers.db"
MAX_SELECT: int = 5
NAUKRI_RECOMMENDED_URL: str = "https://www.naukri.com/mnjuser/recommendedjobs"
# How long to wait for the human to answer an unknown question in the browser.
USER_ANSWER_TIMEOUT_SEC: int = 60

# ============================================================
# Constants — DOM Selectors
# ============================================================
# Naukri changes its markup often; keeping selectors in one place means a
# breakage is a one-line fix instead of a hunt through the file.
SELECTORS: Dict[str, str] = {
    "job_card": "article.jobTuple",
    "job_title": ".title.typ-16Bold",
    # Ordered most-specific first; `_find_card_checkbox` tries each in turn.
    "card_checkbox": (
        ".tuple-check-box i.naukicon, "
        ".tuple-check-box i, "
        ".tuple-check-box, "
        "input[type='checkbox']"
    ),
    "card_checked": (
        ".tuple-check-box i.naukicon-ot-Checked, "
        ".tuple-check-box i.naukicon-ot-checkbox-selected, "
        ".tuple-check-box input[type='checkbox']:checked"
    ),
    "apply_button": "button.multi-apply-button",
    "radio_container": ".singleselect-radiobutton-container",
    "radio_input": ".ssrc__radio",
    "radio_label": ".ssrc__label",
    "drawer": "[id*='ChatbotContainer'], .chatbot_Drawer",
    "drawer_container": "[id*='ChatbotContainer']",
    "bot_message": ".chatbot_ListItem.botItem .msg, .chatbot_ListItem .botMsg .msg",
    "user_message": ".chatbot_ListItem.userItem .msg, .userMsg .msg",
    "tab_list": ".tab-list",
}


# ============================================================
# Utility Functions — Human Behavior Simulation
# ============================================================

def human_wait(min_sec: float = 1.0, max_sec: float = 3.5) -> None:
    """Wait for a random human-like duration between actions.

    Args:
        min_sec: Minimum wait time in seconds.
        max_sec: Maximum wait time in seconds.
    """
    delay = random.uniform(min_sec, max_sec)
    logger.debug(f"Waiting {delay:.2f}s (human-like pause)")
    time.sleep(delay)


def human_type(element: Any, text: str, delay_range: Tuple[float, float] = HUMAN_TYPING_DELAY) -> None:
    """Type text character by character with realistic delays.

    Simulates human typing speed to avoid bot detection.

    Args:
        element: Selenium WebElement to type into.
        text: The text to type.
        delay_range: Min/max delay between characters in seconds.
    """
    logger.debug(f"Human typing: '{text[:50]}{'...' if len(text) > 50 else ''}'")
    for i, char in enumerate(text):
        element.send_keys(char)
        if i < len(text) - 1:
            time.sleep(random.uniform(*delay_range))


def human_click(element: Any, min_wait: float = 0.3, max_wait: float = 1.0) -> None:
    """Click an element after a human-like pause.

    Args:
        element: Selenium WebElement to click.
        min_wait: Minimum pause before clicking.
        max_wait: Maximum pause before clicking.
    """
    human_wait(min_wait, max_wait)
    element.click()


def save_screenshot(driver: webdriver.Edge, label: str = "error") -> str:
    """Capture and save a screenshot for debugging.

    Screenshots are saved to the `screenshots/` directory with a
    timestamped filename.

    Args:
        driver: Active Edge WebDriver instance.
        label: Descriptive label for the screenshot file.

    Returns:
        The file path of the saved screenshot, or empty string on failure.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    filename: str = str(SCREENSHOT_DIR / f"{label}_{timestamp}.png")
    try:
        driver.save_screenshot(filename)
        logger.info(f"📸 Screenshot saved: {filename}")
        return filename
    except Exception as e:
        logger.warning(f"Could not save screenshot: {e}")
        return ""


# Words that carry no meaning when matching a question to a stored answer.
# Dropping them lets "What is your email id?" match "Email ID".
_QUESTION_STOPWORDS = frozenset(["#", "noqa:", "SIM905", "-", "a", "readable", "block", "beats", "a", "set", "literal", "here", "a", "an", "the", "is", "are", "was", "were", "be", "been", "being", "am", "do", "does", "did", "have", "has", "had", "you", "your", "yours", "we", "our", "us", "i", "my", "me", "it", "its", "this", "that", "these", "those", "there", "here", "of", "in", "on", "at", "to", "for", "from", "with", "by", "as", "and", "or", "but", "if", "then", "than", "so", "per", "what", "which", "how", "many", "much", "when", "where", "why", "who", "whom", "whose", "please", "kindly", "mention", "provide", "select", "choose", "share", "most", "appropriate", "option", "options", "yes", "no", "na"])

_LEADING_NUMBER_RE = re.compile(r"^\s*(?:q\s*)?\d+\s*[\.\)\:\-]\s*", re.IGNORECASE)
_RATIO_RE = re.compile(r"(\d)\s*[xX\*\/]\s*(\d)")
_PUNCT_RE = re.compile(r"[^\w\s]")
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_question(text: str) -> str:
    """Lowercase and strip numbering/punctuation so questions compare cleanly."""
    if not text:
        return ""
    normalized = _RATIO_RE.sub(r"\1x\2", text.lower())  # 24*7 / 24/7 → 24x7
    normalized = _LEADING_NUMBER_RE.sub("", normalized)  # "1. Do you..." → "do you..."
    normalized = _PUNCT_RE.sub(" ", normalized)
    return _WHITESPACE_RE.sub(" ", normalized).strip()


def _question_tokens(text: str) -> set:
    """Return the meaningful (non-stopword) tokens of a question."""
    return set(_normalize_question(text).split()) - _QUESTION_STOPWORDS


def _questions_compatible(query_tokens: set, key_tokens: set) -> bool:
    """Reject questions whose distinctive words disagree.

    One question's meaningful words must contain the other's, and the longer
    question may add at most as many words as the shorter one has. This stops
    "experience in Python" from matching "experience in IICS" while still
    allowing "current CTC" to match "current CTC in Lacs per annum".
    """
    if not query_tokens or not key_tokens:
        return False
    if query_tokens <= key_tokens:
        return len(key_tokens - query_tokens) <= len(query_tokens)
    if key_tokens <= query_tokens:
        return len(query_tokens - key_tokens) <= len(key_tokens)
    return False


def _question_similarity(
    query: str, key: str, query_tokens: set, key_tokens: set
) -> float:
    """Blend token coverage with sequence similarity into a 0..1 score."""
    smaller = min(len(query_tokens), len(key_tokens))
    coverage = len(query_tokens & key_tokens) / smaller if smaller else 0.0
    sequence = max(
        difflib.SequenceMatcher(None, query, key).ratio(),
        difflib.SequenceMatcher(
            None, " ".join(sorted(query.split())), " ".join(sorted(key.split()))
        ).ratio(),
    )
    return 0.5 * coverage + 0.5 * sequence


def fuzzy_lookup(
    question_text: str, known_answers: Dict[str, str], cutoff: float = 0.7
) -> Optional[str]:
    """Find the stored answer for the question that best matches `question_text`.

    Matching is case-insensitive and ignores numbering, punctuation and filler
    words. A candidate is only considered when its distinctive words agree with
    the asked question, so "experience in Python" cannot inherit the answer for
    "experience in IICS". Returns None when nothing clears `cutoff`.
    """
    if not question_text or not known_answers:
        return None

    query = _normalize_question(question_text)
    if not query:
        return None
    query_tokens = _question_tokens(question_text)

    best_score = 0.0
    best_answer: Optional[str] = None
    for key, answer in known_answers.items():
        normalized_key = _normalize_question(key)
        if not normalized_key:
            continue
        if normalized_key == query:
            return answer
        key_tokens = _question_tokens(key)
        if not _questions_compatible(query_tokens, key_tokens):
            continue
        score = _question_similarity(query, normalized_key, query_tokens, key_tokens)
        if score > best_score:
            best_score = score
            best_answer = answer

    return best_answer if best_score >= cutoff else None


# ============================================================
# Answer Knowledge Base (SQLite) — self-learning storage
# ============================================================


# In-memory mirror of the answers table so answers learned mid-run are reused
# immediately instead of only after the next tab reload.
_ANSWERS_CACHE: Dict[str, str] = {}


def init_answer_database() -> sqlite3.Connection:
    """
    Initialize the SQLite database and create tables if they don't exist.

    Returns:
        A connection to the answers database.
    """
    conn = sqlite3.connect(ANSWERS_DB)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS answers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question_text TEXT UNIQUE NOT NULL,
            answer TEXT NOT NULL,
            field_type TEXT NOT NULL DEFAULT 'text',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            usage_count INTEGER NOT NULL DEFAULT 0,
            last_used TEXT
        )
        """
    )
    conn.commit()
    return conn


def load_answers() -> Dict[str, str]:
    """
    Load question→answer mappings from SQLite.

    Returns a dict: {question_text: answer}.

    Returns:
        A dictionary mapping question text to answer text.
    """
    loaded: Dict[str, str] = {}
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = init_answer_database()
        cursor = conn.execute(
            "SELECT question_text, answer FROM answers WHERE question_text IS NOT NULL AND answer IS NOT NULL"
        )
        for question_text, answer in cursor.fetchall():
            loaded[question_text.strip()] = answer.strip()

        _ANSWERS_CACHE.clear()
        _ANSWERS_CACHE.update(loaded)
        logger.info(f"Loaded {len(_ANSWERS_CACHE)} known answers from {ANSWERS_DB}")
    except Exception as e:
        logger.warning(f"Could not load {ANSWERS_DB}: {e}")
    finally:
        if conn is not None:
            conn.close()

    return _ANSWERS_CACHE


def save_answer(question_text: str, answer: str, field_type: str = "text") -> None:
    """
    Save or update a question→answer pair in SQLite.

    Args:
        question_text: The question text.
        answer: The answer text.
        field_type: Type of field (text, radio, etc.).
    """
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = init_answer_database()
        conn.execute(
            """
            INSERT INTO answers
                (question_text, answer, field_type, usage_count, last_used)
            VALUES (?, ?, ?, 1, CURRENT_TIMESTAMP)
            ON CONFLICT(question_text) DO UPDATE SET
                answer = excluded.answer,
                field_type = excluded.field_type,
                usage_count = usage_count + 1,
                last_used = CURRENT_TIMESTAMP
            """,
            (question_text.strip(), answer.strip(), field_type),
        )
        conn.commit()
        _ANSWERS_CACHE[question_text.strip()] = answer.strip()
        logger.info(f"💾 Saved: '{question_text}' → '{answer}'")
    except Exception as e:
        logger.error(f"Could not save to {ANSWERS_DB}: {e}")
    finally:
        if conn is not None:
            conn.close()


# ============================================================
# Browser Setup
# ============================================================

def create_edge_driver() -> webdriver.Edge:
    """Create and return an Edge WebDriver instance."""
    options = webdriver.EdgeOptions()
    options.add_argument("--start-maximized")
    options.add_argument("--disable-notifications")

    if WEBDRIVER_MANAGER_AVAILABLE:
        logger.info("Using webdriver-manager to auto-download EdgeDriver...")
        service = EdgeService(EdgeChromiumDriverManager().install())
    else:
        service = EdgeService()

    driver = webdriver.Edge(service=service, options=options)
    return driver


def login_naukri(driver: webdriver.Edge) -> None:
    """Log in to Naukri.com with human-like typing behavior.

    Args:
        driver: Active Edge WebDriver instance.
    """
    logger.info("Logging in to Naukri.com...")
    driver.get('https://login.naukri.com/')  # Could also be a constant
    human_wait(2, 4)

    WebDriverWait(driver, 10).until(
        EC.presence_of_element_located((By.ID, 'usernameField'))
    )

    uname = driver.find_element(By.ID, 'usernameField')
    human_type(uname, NAUKRI_EMAIL)
    human_wait(0.5, 1.5)

    passwd = driver.find_element(By.ID, 'passwordField')
    human_type(passwd, NAUKRI_PASSWORD)
    human_wait(0.5, 1.0)
    passwd.send_keys(Keys.ENTER)

    human_wait(5, 9)
    logger.info("Login completed.")


# ============================================================
# Step 1: Select jobs & click Apply
# ============================================================

def _is_card_selected(card: Any) -> bool:
    """Return True if the job card's checkbox is already ticked."""
    checked = card.find_elements(By.CSS_SELECTOR, SELECTORS["card_checked"])
    return any(el.is_displayed() for el in checked)


def _find_card_checkbox(card: Any) -> Optional[Any]:
    """Find the clickable checkbox element inside a job card.

    Naukri renders the checkbox as an icon inside `.tuple-check-box`, but the
    icon markup changes between releases, so fall back to the wrapper and to
    a real `<input type="checkbox">` if present.
    """
    for selector in SELECTORS["card_checkbox"].split(", "):
        elements = card.find_elements(By.CSS_SELECTOR, selector)
        for el in elements:
            if el.is_displayed():
                return el
    return None


def select_jobs_and_apply(driver: webdriver.Edge, max_jobs: int = MAX_SELECT) -> int:
    """
    Select up to `max_jobs` jobs by clicking their checkboxes, then click Apply.

    Assumes we're already on the recommended jobs page.

    Returns:
        The number of jobs Apply was clicked for (0 if nothing was applied).
    """
    logger.info("=" * 50)
    logger.info("STEP 1: Selecting jobs...")
    logger.info("=" * 50)

    try:
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, SELECTORS["job_card"]))
        )
    except TimeoutException:
        logger.error("No job cards found.")
        return 0

    job_cards = driver.find_elements(By.CSS_SELECTOR, SELECTORS["job_card"])
    logger.info(f"Found {len(job_cards)} job cards.")

    if not job_cards:
        logger.warning("No recommended jobs available.")
        return 0

    selected = 0
    cards_without_checkbox = 0
    for card in job_cards:
        if selected >= min(max_jobs, MAX_SELECT):
            break
        try:
            if _is_card_selected(card):
                continue

            checkbox = _find_card_checkbox(card)
            if checkbox is None:
                cards_without_checkbox += 1
                continue

            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", card)
            human_wait(0.3, 1.0)

            driver.execute_script("arguments[0].click();", checkbox)
            selected += 1
            human_wait(0.5, 1.5)

            try:
                title = card.find_element(By.CSS_SELECTOR, SELECTORS["job_title"]).text
            except NoSuchElementException:
                title = "Unknown"
            logger.info(f"  [{selected}/{min(max_jobs, MAX_SELECT)}] ✓ {title}")
            human_wait(0.5, 1.0)
        except Exception as e:
            logger.warning(f"  Select error: {e}")

    if cards_without_checkbox:
        logger.warning(
            f"  {cards_without_checkbox} card(s) had no visible checkbox — "
            "the page markup may have changed."
        )

    if selected == 0:
        if cards_without_checkbox == len(job_cards):
            logger.error(
                "No selectable checkboxes found on any card. This is a selector "
                "problem, not an empty job list — stopping so we don't skip jobs."
            )
            save_screenshot(driver, label="no_checkboxes_found")
        else:
            logger.info("No unapplied jobs left on this tab.")
        return 0

    try:
        human_wait(1, 3)
        apply_btn = WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, SELECTORS["apply_button"]))
        )
        if apply_btn.get_attribute('disabled') is not None:
            logger.warning("Apply button disabled after selection.")
            return 0

        human_click(apply_btn)
        logger.info(f"✓ Clicked Apply for {selected} jobs!")
        human_wait(2, 5)
        return selected

    except (TimeoutException, ElementClickInterceptedException) as e:
        logger.error(f"Could not click Apply: {e}")
        save_screenshot(driver, label="apply_click_failed")
        return 0


# ============================================================
# Step 2: Handle the Chatbot Drawer Questions
# ============================================================

def handle_chatbot_drawer(driver: webdriver.Edge, known_answers: Dict[str, str]) -> bool:
    """
    After clicking Apply, the chatbot drawer appears.

    Detection order every iteration:
    1. Radio buttons visible → auto-fill or log "unknown" + wait for browser click
    2. Contenteditable text div visible → auto-fill or wait for user to type in browser
    3. Save button ready → click it
    4. Drawer closed → done

    Args:
        driver: Active Edge WebDriver instance.
        known_answers: Pre-loaded question→answer mappings.

    Returns:
        True if the drawer closed normally, False on timeout.
    """
    logger.info("=" * 50)
    logger.info("STEP 2: Chatbot questionnaire...")
    logger.info("=" * 50)

    # Wait for the chatbot drawer
    try:
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, SELECTORS["drawer"]))
        )
        logger.info("Chatbot drawer appeared.")
        human_wait(2, 4)
    except TimeoutException:
        logger.info("No chatbot drawer. Application may be complete.")
        return True

    # Track the last known bot message count so we can detect new questions
    last_bot_msg_count = 0

    for _ in range(60):  # 60 * 2s = up to 2 min waiting
        time.sleep(2)

        try:
            # If drawer closed, we're done
            if not is_drawer_open(driver):
                logger.info("Drawer closed. ✓")
                return True

            # --- 1. Radio buttons ---
            if handle_radio_buttons(driver, known_answers):
                continue

            # --- 2. Contenteditable text field (chatbot free-text input) ---
            if handle_contenteditable(driver, known_answers):
                continue

            # --- 3. Save button ready? ---
            try:
                save_btn = find_save_button(driver)
                if save_btn and click_save_button(driver, save_btn):
                    logger.info("Save button ready. Clicking...")
                    human_wait(2, 4)
                    continue
            except Exception:
                pass

            # --- 4. If none of the above, log and wait for user to interact ---
            bot_msgs = driver.find_elements(By.CSS_SELECTOR, SELECTORS["bot_message"])
            if len(bot_msgs) > last_bot_msg_count:
                for msg in bot_msgs[last_bot_msg_count:]:
                    text = msg.text.strip()
                    if text:
                        logger.info(f"🤖 Bot asks: '{text}'")
                last_bot_msg_count = len(bot_msgs)

            user_msg_count = len(driver.find_elements(By.CSS_SELECTOR, SELECTORS["user_message"]))
            if user_msg_count > 0:
                logger.info(f"  Detected {user_msg_count} user answers. Waiting for next question...")

        except StaleElementReferenceException:
            # DOM re-rendered — retry on next iteration
            continue
        except Exception as e:
            # Catch any unexpected errors to prevent crash
            logger.warning(f"  Iteration error (retrying): {e}")
            continue

    logger.warning("Timed out waiting for drawer to complete.")
    save_screenshot(driver, label="drawer_timeout")
    return False


def is_drawer_open(driver: webdriver.Edge) -> bool:
    """Check if the chatbot drawer is still visible with active content."""
    try:
        containers = driver.find_elements(By.CSS_SELECTOR, SELECTORS["drawer_container"])
        return any(c.is_displayed() for c in containers)
    except Exception:
        return False


def get_latest_bot_question(driver: webdriver.Edge) -> Optional[str]:
    """Get the text of the most recent bot question message."""
    try:
        msgs = driver.find_elements(By.CSS_SELECTOR,
            ".chatbot_ListItem.botItem .msg, .chatbot_ListItem .botMsg .msg")
        if msgs:
            return msgs[-1].text.strip()
    except Exception:
        pass
    return None


def get_latest_user_answer(driver: webdriver.Edge) -> Optional[str]:
    """
    Get the text of the most recent user answer submitted (for learning).

    Returns None if no user message found.
    """
    try:
        msgs = driver.find_elements(By.CSS_SELECTOR,
            ".chatbot_ListItem.userItem .msg, .userMsg .msg")
        if msgs:
            return msgs[-1].text.strip()
    except Exception:
        pass
    return None


def get_user_message_count(driver: webdriver.Edge) -> int:
    """Count how many user messages are in the chat."""
    try:
        return len(driver.find_elements(By.CSS_SELECTOR,
            ".chatbot_ListItem.userItem .msg, .userMsg .msg"))
    except Exception:
        return 0


# ============================================================
# Handler: Radio Buttons
def _click_save_after_radio(driver: webdriver.Edge) -> None:
    """Click the save button right after selecting a radio option.

    Args:
        driver: Active Edge WebDriver instance.
    """
    try:
        save_btn = find_save_button(driver)
        if save_btn and click_save_button(driver, save_btn):
            logger.info("   Clicked Save after radio selection.")
            human_wait(1.5, 3.0)
    except Exception:
        pass


# ============================================================

def _normalize_option(text: str) -> str:
    """Lowercase and strip punctuation/whitespace so options compare cleanly."""
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


def _match_option(answer: str, options: List[str]) -> Optional[int]:
    """Return the index of the option that best matches `answer`, or None.

    Tries an exact (normalized) match first, then a containment match, then a
    fuzzy match. The fuzzy cutoff is deliberately strict: picking the wrong
    radio option silently submits a false answer to a recruiter.
    """
    if not answer or not options:
        return None

    target = _normalize_option(answer)
    normalized = [_normalize_option(o) for o in options]

    for idx, option in enumerate(normalized):
        if option == target:
            return idx

    # Avoid substring matches (e.g. "Yes" matching "Yes, with relocation"):
    # a stored answer should only be selected when the offered option is clear.
    scores = [difflib.SequenceMatcher(None, target, option).ratio() for option in normalized]
    ranked = sorted(enumerate(scores), key=lambda item: item[1], reverse=True)
    if ranked and ranked[0][1] >= 0.85:
        if len(ranked) == 1 or ranked[0][1] - ranked[1][1] >= 0.08:
            return ranked[0][0]
    return None


def _read_radio_options(container: Any) -> Tuple[List[Any], List[str]]:
    """Return (radio elements, option labels) for the visible radio question."""
    radios = container.find_elements(By.CSS_SELECTOR, SELECTORS["radio_input"])
    labels = container.find_elements(By.CSS_SELECTOR, SELECTORS["radio_label"])
    options = [
        (lbl.text.strip() or (rb.get_attribute('value') or '').strip())
        for rb, lbl in zip(radios, labels, strict=False)
    ]
    return radios, options


def _selected_radio_label(container: Any) -> Optional[str]:
    """Return the label of the currently checked radio option, if any."""
    try:
        radios = container.find_elements(By.CSS_SELECTOR, SELECTORS["radio_input"])
        labels = container.find_elements(By.CSS_SELECTOR, SELECTORS["radio_label"])
        for rb, lbl in zip(radios, labels, strict=False):
            if rb.is_selected():
                return lbl.text.strip()
    except Exception:
        pass
    return None


def handle_radio_buttons(driver: webdriver.Edge, known_answers: Dict[str, str]) -> bool:
    """
    Check for a radio button question. If found:
      - Known answer → auto-click the matching option and save
      - Unknown answer → log it, wait for the user to click in the browser, then save

    Returns:
        True if the question was handled (answered or handed to the user),
        False if no radio question is currently on screen.
    """
    try:
        container = driver.find_element(By.CSS_SELECTOR, SELECTORS["radio_container"])
        if not container.is_displayed():
            return False
    except NoSuchElementException:
        return False

    question_text = get_latest_bot_question(driver) or "RadioQuestion"
    radios, options = _read_radio_options(container)
    if not radios:
        return False

    logger.info(f"📋 Radio: '{question_text}'")
    logger.debug(f"   Options: {options}")

    known = fuzzy_lookup(question_text, known_answers)
    if known:
        idx = _match_option(known, options)
        if idx is not None:
            try:
                human_click(radios[idx])
                logger.info(f"   ✓ Auto-selected from memory: '{options[idx]}'")
                human_wait(0.5, 1.5)
                _click_save_after_radio(driver)
                return True
            except Exception as e:
                logger.warning(f"   Could not click remembered option: {e}")
        else:
            logger.warning(
                f"   Remembered answer '{known}' is not one of the options "
                f"{options} — asking you instead."
            )

    # Unknown → wait for the user to click an option in the browser.
    logger.info("   ❓ Unknown radio question.")
    logger.info("   👆 Click your answer in the browser...")

    waited = 0
    while waited < USER_ANSWER_TIMEOUT_SEC:
        time.sleep(2)
        waited += 2
        try:
            if not is_drawer_open(driver):
                return True
            chosen = _selected_radio_label(container)
            if chosen:
                logger.info(f"   ✓ User selected: '{chosen}'")
                save_answer(question_text, chosen, "radio")
                human_wait(0.5, 1.5)
                _click_save_after_radio(driver)
                return True
        except StaleElementReferenceException:
            return True  # DOM re-rendered; let the drawer loop re-read it
        except Exception:
            continue

        if waited % 10 == 0:
            logger.info(f"   Still waiting for you to click an option... ({waited}s)")

    logger.warning("   Timed out waiting for a radio answer.")
    save_screenshot(driver, label="radio_timeout")
    return True


# ============================================================
# Handler: Contenteditable (Chatbot Free-Text Input)
# ============================================================

def handle_contenteditable(driver: webdriver.Edge, known_answers: Dict[str, str]) -> bool:
    """
    Check for the chatbot's free-text input (div[contenteditable="true"]).

    Known answer → auto-type and click save.
    Unknown → log, wait for user to type & submit in browser, extract & save.

    Args:
        driver: Active Edge WebDriver instance.
        known_answers: Pre-loaded question→answer mappings.

    Returns:
        True if handled or drawer closed, False on timeout.
    """
    text_area = find_chat_text_area(driver)
    if not text_area:
        return False

    question_text = get_latest_bot_question(driver) or "ChatbotTextQuestion"
    logger.info(f"📝 Text question: '{question_text}'")

    # KNOWN ANSWER → auto-fill with human-like typing
    known = fuzzy_lookup(question_text, known_answers)
    if known:
        try:
            fresh_text_area = find_chat_text_area(driver)
            if fresh_text_area:
                human_click(fresh_text_area, 0.2, 0.8)
                fresh_text_area.clear()
                human_type(fresh_text_area, known)
                logger.info(f"   ✓ Auto-filled: '{known}'")
                human_wait(0.3, 1.0)
                try:
                    fresh_text_area.send_keys(Keys.RETURN)
                except Exception:
                    pass
                human_wait(0.5, 1.5)
        except Exception as e:
            logger.warning(f"   Could not auto-fill text: {e}")

        # Try clicking Save (with fresh reference)
        try:
            save_btn = find_save_button(driver)
            if save_btn and click_save_button(driver, save_btn):
                logger.info("   Clicked Save after auto-fill.")
                human_wait(2, 4)
                return True
        except Exception:
            pass
        return True

    # UNKNOWN → log and wait for user to type & submit in browser
    current_user_msg_count = get_user_message_count(driver)
    logger.info("   ❓ Unknown text question.")
    logger.info("   ✏️ Type your answer in the browser and click Send/Save...")

    waited = 0
    while waited < 60:
        time.sleep(2)
        waited += 2

        try:
            # Did user submit an answer? (user message count increased)
            new_count = get_user_message_count(driver)
            if new_count > current_user_msg_count:
                answer = get_latest_user_answer(driver)
                if answer:
                    logger.info(f"   ✓ User answered: '{answer}'")
                    save_answer(question_text, answer, "text")
                    time.sleep(2)
                    return True

            if not is_drawer_open(driver):
                return True
        except StaleElementReferenceException:
            # DOM re-rendered — retry next iteration
            continue
        except Exception:
            continue

        if waited % 10 == 0:
            logger.info(f"   Still waiting for you to type & send... ({waited}s)")

    logger.warning("   Timed out waiting for text answer.")
    if driver:
        save_screenshot(driver, label="text_timeout")
    return False


def click_save_button(driver: webdriver.Edge, save_btn: Any) -> bool:
    """Click the save/send button, tolerating a missing 'send' ancestor.

    Args:
        driver: Active Edge WebDriver instance.
        save_btn: The save/send WebElement.

    Returns:
        True if click was attempted, False if button was disabled.
    """
    try:
        parent = save_btn.find_element(By.XPATH, "./ancestor::*[contains(@class,'send')]")
        if 'disabled' in (parent.get_attribute('class') or ''):
            return False  # genuinely disabled
    except Exception:
        pass  # no such ancestor — try clicking anyway
    human_click(save_btn, 0.2, 0.8)
    return True


def find_chat_text_area(driver: webdriver.Edge) -> Optional[Any]:
    """
    Find the chatbot's free-text input area inside the drawer.

    It's a div with contenteditable="true", not a standard input.

    Returns:
        The text area WebElement if found, None otherwise.
    """
    try:
        # Strategy 1: The chatbot input container inside the drawer
        elements = driver.find_elements(By.CSS_SELECTOR,
            ".chatbot_SendMessageContainer .textArea[contenteditable='true'], "
            ".chatbot_InputContainer .textArea[contenteditable='true'], "
            "#userInput__g651km1xsInputBox, "
            "div.textArea[contenteditable='true']"
        )
        for el in elements:
            if el.is_displayed() and el.is_enabled():
                return el
    except Exception:
        pass
    return None


def find_save_button(driver: webdriver.Edge) -> Optional[Any]:
    """
    Find the Save/Send button in the chatbot footer.

    Uses generic selectors since the IDs are dynamic.

    Returns:
        The save/send WebElement if found, None otherwise.
    """
    try:
        selectors = [
            ".sendMsg",
            ".sendMsgbtn_container .sendMsg",
            "[class*='sendMsg']",
            "div.sendMsg",
        ]
        for sel in selectors:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            for el in els:
                if el.is_displayed():
                    return el
    except Exception:
        pass
    return None


# ============================================================
# Tab Selection (terminal prompt)
# ============================================================

def select_tabs() -> List[Dict[str, Any]]:
    """Prompt the user to pick which job tabs to apply to. Returns list of tab dicts."""
    print("\n" + "=" * 50)
    print("Select job tabs to apply to:")
    for t in TABS:
        print(f"  {t['num']}. {t['label']}")
    print("=" * 50)
    raw = input("Enter numbers (e.g. 1,3,5) or press Enter for Top Candidate: ").strip()

    if not raw:
        return [TABS[2]]  # default: Top Candidate

    selected = []
    for part in raw.split(','):
        part = part.strip()
        if part.isdigit():
            idx = int(part) - 1
            if 0 <= idx < len(TABS):
                selected.append(TABS[idx])

    if not selected:
        print("Invalid input — defaulting to Top Candidate.")
        return [TABS[2]]

    return selected


def navigate_to_tab(driver: webdriver.Edge, tab_id: str) -> bool:
    """Navigate to the recommended jobs page and activate the given tab.

    Returns:
        True if the tab was activated, False otherwise. Callers must not
        apply to jobs when this fails, or they will apply to whatever tab
        happens to be active instead.
    """
    driver.get(NAUKRI_RECOMMENDED_URL)
    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, SELECTORS["tab_list"]))
        )
        time.sleep(2)
        # Click the inner label, not the wrapper div
        tab_el = driver.find_element(By.CSS_SELECTOR, f"div#{tab_id} .tab-list-item")
        driver.execute_script("arguments[0].scrollIntoView(true);", tab_el)
        driver.execute_script("arguments[0].click();", tab_el)
        logger.info(f"Switched to tab: {tab_id}")
        human_wait(2, 4)
        return True
    except Exception as e:
        logger.error(f"Could not activate tab '{tab_id}': {e}")
        save_screenshot(driver, label=f"tab_failed_{tab_id}")
        return False


# ============================================================
# Main
# ============================================================

def main() -> None:
    """Main entry point — loops until no recommended jobs remain."""
    logger.info("=" * 50)
    logger.info("Naukri Recommended Jobs Bot")
    logger.info("=" * 50)

    if not NAUKRI_EMAIL or not NAUKRI_PASSWORD:
        logger.error("Set NAUKRI_EMAIL and NAUKRI_PASSWORD in .env")
        return

    selected_tabs = select_tabs()
    logger.info(f"Selected tabs: {', '.join(t['label'] for t in selected_tabs)}")
    try:
        confirmation = input(
            f"This will submit real applications (up to {MAX_SELECT} per batch). "
            "Type APPLY to continue: "
        ).strip()
    except EOFError:
        logger.error("No interactive confirmation available; stopping safely.")
        return
    if confirmation != "APPLY":
        logger.info("Confirmation not provided; stopping without applying.")
        return

    known_answers = load_answers()
    total_applied = 0

    driver = None
    try:
        driver = create_edge_driver()
        login_naukri(driver)

        for tab in selected_tabs:
            logger.info("=" * 50)
            logger.info(f"Tab: {tab['label']}")
            logger.info("=" * 50)

            while True:
                if not navigate_to_tab(driver, tab['id']):
                    logger.error(f"Skipping tab '{tab['label']}' — could not activate it.")
                    break

                applied = select_jobs_and_apply(driver)
                if applied == 0:
                    logger.info(f"No more jobs in '{tab['label']}'.")
                    break

                handle_chatbot_drawer(driver, known_answers)
                total_applied += applied
                known_answers = load_answers()

                logger.info(f"Total applied so far: {total_applied}")
                human_wait(2, 4)

        logger.info("=" * 50)
        logger.info(f"✅ Complete! Applied to {total_applied} jobs total.")
        logger.info("=" * 50)

    except Exception as e:
        logger.exception(f"Error: {e}")
        if driver:
            try:
                save_screenshot(driver, label="fatal_error")
            except Exception:
                pass
    finally:
        if driver:
            try:
                time.sleep(2)
                driver.quit()
            except Exception:
                pass


if __name__ == "__main__":
    main()
