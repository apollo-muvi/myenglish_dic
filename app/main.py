import json
import logging
import os
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
DB_PATH = Path(os.getenv("DATABASE_PATH", ROOT / "data" / "dictionary.sqlite3"))
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "deepseek/deepseek-v4.1-flash")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
DAILY_LOOKUP_LIMIT = int(os.getenv("DAILY_LOOKUP_LIMIT", "100"))
DATAMUSE_BASE_URL = "https://api.datamuse.com/words"
MAX_SPELLING_SUGGESTIONS = 12

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
if not logger.handlers:
    logger.addHandler(logging.StreamHandler())
logger.propagate = False
app = FastAPI(title="My English Dictionary", version="1.0.0")
app.mount("/static", StaticFiles(directory=ROOT / "app" / "static"), name="static")


class LookupInput(BaseModel):
    word: str = Field(min_length=1, max_length=60)
    include_synonyms: bool = False
    include_word_family: bool = False


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.row_factory = sqlite3.Row
    try:
        yield db
        db.commit()
    finally:
        db.close()


@app.on_event("startup")
def initialize_database() -> None:
    with connect() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS entries (word_key TEXT PRIMARY KEY, requested_word TEXT NOT NULL, entry_json TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS daily_usage (usage_date TEXT PRIMARY KEY, requests INTEGER NOT NULL DEFAULT 0)"
        )


def normalize_word(raw: str) -> str:
    word = " ".join(raw.strip().split())
    if not word or not re.fullmatch(
        r"[A-Za-z]+(?:['’-][A-Za-z]+)*(?: [A-Za-z]+(?:['’-][A-Za-z]+)*)*", word
    ):
        raise HTTPException(422, "請輸入英文字或英文片語。")
    return word


def reserve_daily_request() -> None:
    today = datetime.now(timezone.utc).date().isoformat()
    with connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO daily_usage(usage_date, requests) VALUES(?, 0)",
            (today,),
        )
        row = db.execute(
            "SELECT requests FROM daily_usage WHERE usage_date=?", (today,)
        ).fetchone()
        if row["requests"] >= DAILY_LOOKUP_LIMIT:
            raise HTTPException(429, "今日模型查詢額度已用完，請明天再試。")
        db.execute(
            "UPDATE daily_usage SET requests=requests+1 WHERE usage_date=?", (today,)
        )


def request_model_json(system_prompt: str, user_prompt: str, operation: str) -> dict:
    if not OPENROUTER_API_KEY:
        raise HTTPException(503, "尚未設定字典服務的模型 API 金鑰。")
    try:
        response = httpx.post(
            f"{OPENROUTER_BASE_URL.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": OPENROUTER_MODEL,
                "temperature": 0.2,
                "max_completion_tokens": 1600,
                "reasoning": {"effort": "none"},
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            },
            timeout=60,
        )
        response.raise_for_status()
        provider_result = response.json()
        usage = provider_result.get("usage", {})
        logger.info(
            "Dictionary API operation=%s prompt_tokens=%s completion_tokens=%s cost_usd=%s",
            operation,
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
            usage.get("cost"),
        )
        completion = provider_result["choices"][0]
        content = completion["message"]["content"]
        if not isinstance(content, str):
            raise TypeError("Model response content must be text.")
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.I)
        try:
            result = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            logger.warning(
                "Invalid dictionary JSON operation=%s finish_reason=%s content_length=%s parse_offset=%s",
                operation,
                completion.get("finish_reason", "unknown"),
                len(content),
                exc.pos,
            )
            raise
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "Dictionary provider returned HTTP %s operation=%s",
            exc.response.status_code,
            operation,
        )
        raise HTTPException(502, "字典查詢暫時失敗，請稍後重試。") from exc
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
        logger.warning(
            "Dictionary provider response failed operation=%s error_type=%s",
            operation,
            type(exc).__name__,
        )
        raise HTTPException(502, "字典查詢暫時失敗，請稍後重試。") from exc
    if not isinstance(result, dict):
        raise HTTPException(502, "字典服務回傳格式不正確，請重試。")
    return result


def lookup_with_model(word: str) -> dict:
    result = request_model_json(
        "Create an accurate English learner dictionary entry for a Taiwanese learner. "
        "Return one JSON object with exactly these keys: word, part_of_speech, pronunciation_uk, "
        "pronunciation_us, definition_en, definition_zh, examples. examples must contain exactly "
        "3 objects with sentence, translation_zh, tense, usage_note. Use natural everyday English. "
        "The examples must demonstrate Present Continuous, Past Simple, and Present Perfect, in "
        "that order, with those exact tense labels. If the headword cannot naturally take that form, "
        "use and explain a related form. If the input is inflected, identify its canonical headword. "
        "Use Traditional Chinese for Chinese text.",
        f"Look up: {word}",
        "lookup",
    )
    required = (
        "word",
        "part_of_speech",
        "pronunciation_uk",
        "pronunciation_us",
        "definition_en",
        "definition_zh",
    )
    examples = result.get("examples")
    if any(
        not isinstance(result.get(key), str) or not result[key].strip()
        for key in required
    ):
        raise HTTPException(502, "字典服務回傳的內容不完整，請重試。")
    expected_tenses = ("present continuous", "past simple", "present perfect")
    if (
        not isinstance(examples, list)
        or len(examples) < 3
        or tuple(
            str(item.get("tense", "")).strip().casefold()
            for item in examples[:3]
            if isinstance(item, dict)
        )
        != expected_tenses
        or any(
            not isinstance(item, dict)
            or any(
                not isinstance(item.get(key), str) or not item[key].strip()
                for key in ("sentence", "translation_zh", "tense", "usage_note")
            )
            for item in examples[:3]
        )
    ):
        raise HTTPException(502, "字典服務未提供完整且正確排序的例句，請重試。")
    result["examples"] = examples[:3]
    return result


def lookup_synonyms(word: str) -> dict:
    result = request_model_json(
        "Create a practical Taiwanese learner's English synonym comparison. Return JSON only: "
        "synonyms (array of up to 4 objects with word, meaning_zh, register (spoken or written), "
        "frequency (high, medium, or low), frequency_note_zh, distinction_zh, example, translation_zh), "
        "and core_distinction_zh. Include common, genuinely close synonyms only. Frequency is a "
        "qualitative general-use estimate, never a corpus count. Explain natural usage and distinctions. "
        "Use Traditional Chinese. If no close synonym exists, return an empty synonyms array.",
        f"Compare synonyms for: {word}",
        "synonyms",
    )
    synonyms = result.get("synonyms")
    fields = (
        "word",
        "meaning_zh",
        "register",
        "frequency",
        "frequency_note_zh",
        "distinction_zh",
        "example",
        "translation_zh",
    )
    if (
        not isinstance(synonyms, list)
        or len(synonyms) > 4
        or any(
            not isinstance(item, dict)
            or any(
                not isinstance(item.get(key), str) or not item[key].strip()
                for key in fields
            )
            or item["register"] not in {"spoken", "written"}
            or item["frequency"] not in {"high", "medium", "low"}
            for item in synonyms
        )
    ):
        raise HTTPException(502, "同義字比較結果格式不完整，請重試。")
    core = result.get("core_distinction_zh", "")
    if not isinstance(core, str):
        raise HTTPException(502, "同義字比較結果格式不完整，請重試。")
    return {"synonyms": synonyms, "core_distinction_zh": core}


def lookup_word_family(word: str) -> dict:
    result = request_model_json(
        "You are an accurate English word-family dictionary for Taiwanese learners. Return JSON only: "
        "{forms:[{word,part_of_speech,meaning_zh,pattern,example,translation_zh}]}. Give 1-5 "
        "genuinely useful derived forms in other parts of speech that share the same lexical root, not "
        "synonyms. Explain spelling or usage briefly in Traditional Chinese, and give one natural example "
        "with a Traditional Chinese translation. Do not invent forms; use an empty forms array when none fit.",
        f"Explain useful parts of speech for: {word}",
        "word_family",
    )
    forms = result.get("forms")
    fields = (
        "word",
        "part_of_speech",
        "meaning_zh",
        "pattern",
        "example",
        "translation_zh",
    )
    if (
        not isinstance(forms, list)
        or len(forms) > 5
        or any(
            not isinstance(item, dict)
            or any(
                not isinstance(item.get(key), str) or not item[key].strip()
                for key in fields
            )
            for item in forms
        )
    ):
        raise HTTPException(502, "詞性說明結果格式不完整，請重試。")
    return {"forms": forms}


def spelling_suggestions(word: str) -> list[str]:
    if " " in word or len(word) > 24:
        return []
    patterns = set()
    for index in range(len(word)):
        patterns.add(word[:index] + "?" + word[index + 1 :])
        patterns.add(word[:index] + "?" + word[index:])
    patterns.add(word + "?")
    patterns.add("?" + word)

    def fetch(pattern: str) -> list[dict]:
        try:
            response = httpx.get(
                DATAMUSE_BASE_URL,
                params={"sp": pattern, "max": 20, "md": "f"},
                timeout=8,
            )
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, list) else []
        except (httpx.HTTPError, ValueError):
            return []

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(fetch, sorted(patterns)))

    def edit_distance(left: str, right: str) -> int:
        previous = list(range(len(right) + 1))
        for row, left_char in enumerate(left, start=1):
            current = [row]
            for column, right_char in enumerate(right, start=1):
                current.append(
                    min(
                        current[-1] + 1,
                        previous[column] + 1,
                        previous[column - 1] + (left_char != right_char),
                    )
                )
            previous = current
        return previous[-1]

    best: dict[str, tuple[str, int, int]] = {}
    for response in responses:
        for item in response:
            candidate = str(item.get("word", "")).strip()
            if not re.fullmatch(r"[A-Za-z]+(?:['-][A-Za-z]+)*", candidate):
                continue
            key = candidate.casefold()
            distance = edit_distance(word.casefold(), key)
            if key == word.casefold() or distance > 1:
                continue
            frequency = None
            for tag in item.get("tags", []):
                if isinstance(tag, str) and tag.startswith("f:"):
                    try:
                        frequency = float(tag[2:])
                    except ValueError:
                        pass
                    break
            if frequency is not None and frequency < 0.1:
                continue
            score = item.get("score", 0)
            score = score if isinstance(score, int) else 0
            previous = best.get(key)
            if previous is None or score > previous[2]:
                best[key] = (candidate, distance, score)
    ranked = sorted(
        best.values(),
        key=lambda item: (
            item[1] / max(len(word), len(item[0])),
            -item[2],
            item[0].casefold(),
        ),
    )
    return [item[0] for item in ranked[:MAX_SPELLING_SUGGESTIONS]]


def save_record(word: str, entry: dict, suggestions: list[str]) -> None:
    key = word.casefold()
    payload = {"entry": entry, "similar_spelling": suggestions}
    with connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO entries(word_key, requested_word, entry_json, created_at) VALUES(?,?,?,?)",
            (
                key,
                word,
                json.dumps(payload, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(),
            ),
        )


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(ROOT / "app" / "static" / "index.html")


@app.get("/healthz", include_in_schema=False)
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/lookup")
def lookup(payload: LookupInput) -> dict:
    word = normalize_word(payload.word)
    key = word.casefold()
    with connect() as db:
        cached = db.execute(
            "SELECT entry_json FROM entries WHERE word_key=?", (key,)
        ).fetchone()
    legacy_record = False
    if cached:
        stored = json.loads(cached["entry_json"])
        if isinstance(stored, dict) and "entry" in stored:
            entry = stored["entry"]
            suggestions = stored.get("similar_spelling", [])
        else:  # Upgrade entries created before spelling candidates were persisted.
            legacy_record = True
            entry = stored
            suggestions = spelling_suggestions(word)
    else:
        if not OPENROUTER_API_KEY:
            raise HTTPException(503, "尚未設定字典服務的模型 API 金鑰。")
        reserve_daily_request()
        entry = lookup_with_model(word)
        suggestions = spelling_suggestions(word)
        save_record(word, entry, suggestions)
    if legacy_record and not (payload.include_synonyms or payload.include_word_family):
        save_record(word, entry, suggestions)

    if (payload.include_synonyms and not entry.get("synonym_comparison")) or (
        payload.include_word_family and not entry.get("word_family")
    ):
        if not OPENROUTER_API_KEY:
            raise HTTPException(503, "尚未設定字典服務的模型 API 金鑰。")
    if payload.include_synonyms and not entry.get("synonym_comparison"):
        reserve_daily_request()
        entry["synonym_comparison"] = lookup_synonyms(word)
        save_record(word, entry, suggestions)
    if payload.include_word_family and not entry.get("word_family"):
        reserve_daily_request()
        entry["word_family"] = lookup_word_family(word)
        save_record(word, entry, suggestions)
    return {"entry": entry, "similar_spelling": suggestions, "cached": bool(cached)}
