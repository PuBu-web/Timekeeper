from __future__ import annotations

import difflib
import re
import unicodedata
import threading
import time
import os
from dataclasses import dataclass
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials

import config

FACTION_EMOJI = {"魏": "🔵", "蜀": "🟢", "呉": "🔴"}

def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = value.replace(" ", "").replace(" ", "")
    return value.strip()

def row_value(row: list, index: int):
    """末尾が空欄の行でも安全に値を取得する。"""
    return row[index] if 0 <= index < len(row) else None

def get_gclient():
    """Render環境とローカル環境の両方で安全に認証情報を読み込む"""
    cred_path = "/etc/secrets/credentials.json"
    if not os.path.exists(cred_path):
        cred_path = "credentials.json"
        
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    credentials = Credentials.from_service_account_file(cred_path, scopes=scopes)
    return gspread.authorize(credentials)


@dataclass(frozen=True)
class City:
    name: str
    reading: str
    x: int
    y: int
    category: str
    county: str
    faction: str

    @property
    def coordinate(self) -> str:
        return f"{self.x},{self.y}"

    @property
    def emoji(self) -> str:
        return FACTION_EMOJI.get(self.faction, "⚪")


@dataclass(frozen=True)
class GuildResolution:
    full_name: str
    short_name: str
    registered: bool
    faction: str = ""
    auto_added: bool = False
    save_error: str = ""
    learned: bool = False
    learning_error: str = ""


class SpreadsheetMaster:
    """スプレッドシートを読み込み、API制限を回避しつつ自動保存する。"""

    def __init__(self):
        self.by_name: dict[str, City] = {}
        self.city_aliases: dict[str, str] = {}
        self.guilds: dict[str, tuple[str, str]] = {}
        self.guild_aliases: dict[str, str] = {}
        
        self._last_reload_time = 0.0
        self._write_lock = threading.RLock()
        
        self.last_city_learning: tuple[str, str] | None = None
        self.last_city_learning_error = ""
        self.last_city_rejection_reason = ""
        
        self.reload(force=True)

    def reload(self, force: bool = False) -> bool:
        now = time.time()
        # 60秒以内の連続リロードはキャッシュを使ってAPIコールを節約する
        if not force and (now - self._last_reload_time < 60):
            return False

        with self._write_lock:
            gc = get_gclient()
            sh = gc.open_by_key(config.SPREADSHEET_ID)
            
            # --- 城データの読み込み ---
            city_ws = sh.worksheet("城データ")
            city_data = city_ws.get_all_values()
            if not city_data:
                raise RuntimeError("スプレッドシートの『城データ』が空です")
                
            headers = {normalize_text(str(c or "")): i for i, c in enumerate(city_data[0])}
            required = ["城名", "X", "Y", "郡城", "国"]
            missing = [h for h in required if h not in headers]
            if missing:
                raise RuntimeError("城データの列が不足しています: " + ", ".join(missing))

            cities: dict[str, City] = {}
            for row in city_data[1:]:
                name = normalize_text(str(row_value(row, headers["城名"]) or ""))
                if not name:
                    continue
                try:
                    x = int(row_value(row, headers["X"]))
                    y = int(row_value(row, headers["Y"]))
                except (TypeError, ValueError):
                    continue
                cities[name] = City(
                    name=name,
                    reading=normalize_text(str(row_value(row, headers.get("ふりがな", -1)) or "")) if "ふりがな" in headers else "",
                    x=x,
                    y=y,
                    category=normalize_text(str(row_value(row, headers.get("種類", -1)) or "")) if "種類" in headers else "",
                    county=normalize_text(str(row_value(row, headers["郡城"]) or "")),
                    faction=normalize_text(str(row_value(row, headers["国"]) or "")),
                )

            # --- 城OCR補正の読み込み ---
            city_aliases: dict[str, str] = {}
            try:
                ws = sh.worksheet("城OCR補正")
                data = ws.get_all_values()
                if data:
                    h = {normalize_text(str(c or "")): i for i, c in enumerate(data[0])}
                    if "OCR誤読城名" in h and "正式城名" in h:
                        for row in data[1:]:
                            wrong = normalize_text(str(row_value(row, h["OCR誤読城名"]) or ""))
                            correct = normalize_text(str(row_value(row, h["正式城名"]) or ""))
                            if wrong and correct:
                                city_aliases[wrong] = correct
            except gspread.exceptions.WorksheetNotFound:
                pass

            # --- 軍団データの読み込み ---
            guild_ws = sh.worksheet("軍団データ")
            guild_data = guild_ws.get_all_values()
            gheaders = {normalize_text(str(c or "")): i for i, c in enumerate(guild_data[0])}
            required_guild = ["正式軍団名", "登録名", "所属国"]
            missing_guild = [h for h in required_guild if h not in gheaders]
            if missing_guild:
                raise RuntimeError("軍団データの列が不足しています: " + ", ".join(missing_guild))

            guilds: dict[str, tuple[str, str]] = {}
            for row in guild_data[1:]:
                full = self._core(str(row_value(row, gheaders["正式軍団名"]) or ""))
                short = normalize_text(str(row_value(row, gheaders["登録名"]) or ""))
                faction = normalize_text(str(row_value(row, gheaders["所属国"]) or ""))
                if full and short:
                    guilds[full] = (short, faction)

            # --- 軍団OCR補正の読み込み ---
            guild_aliases: dict[str, str] = {}
            try:
                ws = sh.worksheet("軍団OCR補正")
                data = ws.get_all_values()
                if data:
                    h = {normalize_text(str(c or "")): i for i, c in enumerate(data[0])}
                    if "OCR誤読名" in h and "正式軍団名" in h:
                        for row in data[1:]:
                            wrong = self._core(str(row_value(row, h["OCR誤読名"]) or ""))
                            correct = self._core(str(row_value(row, h["正式軍団名"]) or ""))
                            if wrong and correct:
                                guild_aliases[wrong] = correct
            except gspread.exceptions.WorksheetNotFound:
                pass

            self.by_name = cities
            self.city_aliases = city_aliases
            self.guilds = guilds
            self.guild_aliases = guild_aliases
            self._last_reload_time = now
            return True

    @property
    def city_count(self) -> int:
        self.reload()
        return len(self.by_name)

    @property
    def guild_count(self) -> int:
        self.reload()
        return len(self.guilds)

    def find_city(self, raw_name: str, raw_coordinate: str = "") -> City | None:
        self.reload()
        self.last_city_learning = None
        self.last_city_learning_error = ""
        self.last_city_rejection_reason = ""

        original_name = normalize_text(raw_name)
        name = re.sub(r"^(?:県城|郡城|港|関所|城)?Lv\d+", "", original_name, flags=re.IGNORECASE)
        name = re.sub(r"^(?:県城|郡城)", "", name)
        alias_corrected = self.city_aliases.get(name, name)

        coord = normalize_text(raw_coordinate).replace(".", ",")
        cm = re.fullmatch(r"(\d{1,4})\s*[,，]\s*(\d{1,4})", coord)
        coordinate_was_supplied = bool(coord)
        matched: City | None = None

        if cm:
            x, y = int(cm.group(1)), int(cm.group(2))
            exact = [c for c in self.by_name.values() if c.x == x and c.y == y]
            if len(exact) == 1:
                matched = exact[0]
            else:
                nearby = [c for c in self.by_name.values() if abs(c.x - x) <= 3 and abs(c.y - y) <= 3]
                if len(nearby) == 1:
                    matched = nearby[0]
                elif len(nearby) > 1:
                    ranked = sorted(
                        nearby,
                        key=lambda c: (
                            abs(c.x - x) + abs(c.y - y),
                            -difflib.SequenceMatcher(None, alias_corrected, c.name).ratio(),
                        ),
                    )
                    best, second = ranked[0], ranked[1]
                    best_key = (
                        abs(best.x - x) + abs(best.y - y),
                        -difflib.SequenceMatcher(None, alias_corrected, best.name).ratio(),
                    )
                    second_key = (
                        abs(second.x - x) + abs(second.y - y),
                        -difflib.SequenceMatcher(None, alias_corrected, second.name).ratio(),
                    )
                    if best_key < second_key:
                        matched = best

            if matched is None:
                expected = self.by_name.get(alias_corrected)

                if expected is not None:
                    ocr_x, ocr_y = str(x), str(y)
                    master_x, master_y = str(expected.x), str(expected.y)

                    one_digit_coordinate_error = False
                    if len(ocr_x) == len(master_x) and len(ocr_y) == len(master_y):
                        diff_count = (
                            sum(a != b for a, b in zip(ocr_x, master_x))
                            + sum(a != b for a, b in zip(ocr_y, master_y))
                        )
                        if diff_count == 1:
                            one_digit_coordinate_error = True

                    if one_digit_coordinate_error:
                        matched = expected
                    else:
                        self.last_city_rejection_reason = (
                            f"城名『{name}』は登録済みですが、OCR座標 {coord} と "
                            f"正式座標 {expected.coordinate} が大きく異なります"
                        )
                        return None
                else:
                    self.last_city_rejection_reason = f"OCR座標 {coord} に一致・近接する城がありません"
                    return None
        elif coordinate_was_supplied:
            self.last_city_rejection_reason = f"座標形式が不完全です: {raw_coordinate}"
            return None
        else:
            if alias_corrected in self.by_name:
                matched = self.by_name[alias_corrected]
            else:
                matches = difflib.get_close_matches(alias_corrected, self.by_name.keys(), n=1, cutoff=0.82)
                if matches:
                    matched = self.by_name[matches[0]]

        if matched is None:
            self.last_city_rejection_reason = f"城名『{name}』を安全に特定できません"
            return None

        similarity = difflib.SequenceMatcher(None, alias_corrected, matched.name).ratio() if alias_corrected else 0.0
        if cm and alias_corrected and alias_corrected != matched.name and similarity < 0.45:
            self.last_city_rejection_reason = (
                f"座標は {matched.name}{matched.coordinate} ですが、OCR城名『{name}』との一致度が低すぎます"
            )
            return None

        if matched is not None and name and name != matched.name and name not in self.city_aliases:
            try:
                if self._append_learning("城OCR補正", "OCR誤読城名", "正式城名", name, matched.name):
                    self.last_city_learning = (name, matched.name)
            except Exception as exc:
                self.last_city_learning_error = str(exc)

        return matched

    @staticmethod
    def _core(name: str) -> str:
        name = normalize_text(name)
        name = re.sub(r"(?:軍軍団|軍団)$", "", name)
        name = re.sub(r"^.{1,6}?国", "", name)
        return name.strip()

    def resolve_guild(self, raw_name: str, auto_add: bool = True) -> GuildResolution:
        self.reload()
        core = self._core(raw_name)
        corrected = self.guild_aliases.get(core, core)

        if corrected in self.guilds:
            short, faction = self.guilds[corrected]
            return GuildResolution(corrected, short, True, faction=faction)

        registered = ""
        candidates = sorted(self.guilds, key=len, reverse=True)
        for candidate in candidates:
            if candidate in corrected or corrected in candidate:
                registered = candidate
                break

        if not registered:
            close = difflib.get_close_matches(corrected, self.guilds.keys(), n=1, cutoff=0.82)
            if close:
                registered = close[0]

        if registered:
            short, faction = self.guilds[registered]
            learned = False
            learning_error = ""
            if core and core != registered and core not in self.guild_aliases:
                try:
                    learned = self._append_learning(
                        "軍団OCR補正", "OCR誤読名", "正式軍団名", core, registered
                    )
                except Exception as exc:
                    learning_error = str(exc)
            return GuildResolution(
                registered, short, True, faction=faction,
                learned=learned, learning_error=learning_error,
            )

        fallback = corrected[:2] if corrected else "不明"
        if not auto_add or not corrected:
            return GuildResolution(corrected, fallback, False)

        try:
            self._append_unknown_guild(corrected, fallback)
            return GuildResolution(corrected, fallback, False, auto_added=True)
        except Exception as exc:
            return GuildResolution(corrected, fallback, False, save_error=str(exc))

    def _append_learning(
        self, sheet_name: str, wrong_header: str, correct_header: str, wrong: str, correct: str
    ) -> bool:
        wrong = normalize_text(wrong)
        correct = normalize_text(correct)
        if not wrong or not correct or wrong == correct:
            return False

        with self._write_lock:
            gc = get_gclient()
            sh = gc.open_by_key(config.SPREADSHEET_ID)
            try:
                ws = sh.worksheet(sheet_name)
            except gspread.exceptions.WorksheetNotFound:
                ws = sh.add_worksheet(title=sheet_name, rows=100, cols=20)
                ws.append_row([wrong_header, correct_header, "メモ"])
            
            all_data = ws.get_all_values()
            if not all_data:
                ws.append_row([wrong_header, correct_header, "メモ"])
                all_data = ws.get_all_values()
                
            headers = {normalize_text(str(c or "")): i for i, c in enumerate(all_data[0])}
            wrong_idx = headers.get(wrong_header, -1)
            correct_idx = headers.get(correct_header, -1)
            memo_idx = headers.get("メモ", -1)
            
            if wrong_idx == -1 or correct_idx == -1:
                return False

            for row in all_data[1:]:
                if len(row) > wrong_idx and normalize_text(str(row[wrong_idx] or "")) == wrong:
                    return False

            new_row = [""] * max(len(headers), 3)
            new_row[wrong_idx] = wrong
            new_row[correct_idx] = correct
            if memo_idx != -1:
                new_row[memo_idx] = "学習版が自動登録"
                
            ws.append_row(new_row)

        self.reload(force=True)
        return True

    def _append_unknown_guild(self, full_name: str, short_name: str) -> None:
        with self._write_lock:
            gc = get_gclient()
            sh = gc.open_by_key(config.SPREADSHEET_ID)
            ws = sh.worksheet("軍団データ")
            
            all_data = ws.get_all_values()
            headers = {normalize_text(str(c or "")): i for i, c in enumerate(all_data[0])}
            name_idx = headers.get("正式軍団名", -1)
            
            if name_idx == -1:
                return
                
            existing = {
                self._core(str(row[name_idx] or ""))
                for row in all_data[1:] if len(row) > name_idx
            }
            
            if full_name not in existing:
                new_row = [""] * len(headers)
                new_row[name_idx] = full_name
                new_row[headers.get("登録名", 1)] = short_name
                if "所属国" in headers:
                    new_row[headers["所属国"]] = ""
                
                # 適当な空き列にメモを入れる（一番右の列を想定）
                new_row[-1] = "未登録のため自動追加。所属国を入力してください"
                ws.append_row(new_row)
                
        self.reload(force=True)

    def short_guild_name(self, raw_name: str) -> str:
        return self.resolve_guild(raw_name, auto_add=False).short_name

    def guild_emoji(self, raw_name: str, fallback_faction: str = "") -> str:
        result = self.resolve_guild(raw_name, auto_add=False)
        return FACTION_EMOJI.get(result.faction or fallback_faction, "⚪")


# --- シングルトン化してAPIコールを削減 ---
_shared_master = None

def get_shared_master():
    global _shared_master
    if _shared_master is None:
        _shared_master = SpreadsheetMaster()
    return _shared_master


class CityMaster:
    def __init__(self, xlsx_path: Path = None):
        self.master = get_shared_master()

    def find(self, raw_name: str, raw_coordinate: str = "") -> City | None:
        return self.master.find_city(raw_name, raw_coordinate)

    @property
    def last_learning(self) -> tuple[str, str] | None:
        return self.master.last_city_learning

    @property
    def last_learning_error(self) -> str:
        return self.master.last_city_learning_error

    @property
    def last_rejection_reason(self) -> str:
        return self.master.last_city_rejection_reason

    @property
    def count(self) -> int:
        return self.master.city_count


class GuildMaster:
    def __init__(self, xlsx_path: Path = None):
        self.master = get_shared_master()

    def resolve(self, raw_name: str, auto_add: bool = True) -> GuildResolution:
        return self.master.resolve_guild(raw_name, auto_add)

    def short_name(self, raw_name: str) -> str:
        return self.master.short_guild_name(raw_name)

    def emoji(self, raw_name: str, fallback_faction: str = "") -> str:
        return self.master.guild_emoji(raw_name, fallback_faction)

    @property
    def count(self) -> int:
        return self.master.guild_count
