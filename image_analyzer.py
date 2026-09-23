from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OccupationRaw:
    timestamp: str
    guild_name: str
    city_name: str
    coordinate: str = ""
    is_complete: bool = True


PROMPT = r"""
あなたは「新三國志」のゲーム内チャット画像を読み取る専用解析器です。
画像の中から、「占領しました。」と書かれた占領ログだけを抽出してください。
文章が吹き出し内で自動的に改行され、「占領しまし」と「た。」が別の行になっていても、
吹き出し全体が画像内に見えている場合は正常な占領ログとして抽出してください。
通常の会話、攻撃、偵察、移動、ログイン、撤退、準備、占領以外のログは完全に無視してください。

各占領ログについて、必ず同じ吹き出し・同じログブロックに属する情報だけを組み合わせてください。
- timestamp: 対象の占領吹き出しの「すぐ上・中央付近」に表示された白い日時
- guild_name: 占領本文にある軍団名。国名と末尾の「軍団」も含め、見える文字をできる限りそのまま
- city_name: 赤色の城名だけ。「県城」「郡城」「王城」「港」「Lv1〜Lv10」などは含めない
- coordinate: 緑色の座標。必ず X,Y 形式。読めなければ空文字
- is_complete: ログ本文が最後の「占領しました。」まで画像内に完全に見えている場合だけ true

【Version4.1 見切れ対策・最重要】
- 画像の下端・上端で本文が途中までしか見えないログは絶対に抽出しない
- 時刻だけ見えていても、本文の先頭または末尾が切れていたら抽出しない
- 「おめでとうございます！」の一部、軍団名の一部、城名の一部、座標の一部しか見えない行は抽出しない
- 本文末尾の「占領しました。」まで確認できないログは抽出しない
- ただし、吹き出し内で文章が折り返され、「占領しまし」の次の行に「た。」と表示されるのは正常な改行
- 「占領しまし」＋改行＋「た。」は「占領しました。」として扱う
- 最後の「た。」まで画像内に見えていれば、見切れではなく完全なログとして抽出する
- 見切れた文字を推測して、城名・座標・軍団名を補完しない
- 少しでも見切れ・不完全の疑いがあれば、そのログ自体をrecordsへ入れない

【時刻の最重要ルール】
- timestampは必ず秒まである「HH:MM:SS」を含むものだけを返す
- 正式な例: 2026/07/30 21:09:32、21:09:32
- 不正な例: 2026-07-30 21:10、21:10（秒がないため絶対に採用しない）
- 半透明チャットの背後に見えるゲーム画面の時計・日付・透かし文字は無視する
- 左端や背景に重なった「YYYY-MM-DD HH:MM」形式はチャット時刻ではない
- 対象の占領吹き出しの直上にある白文字の秒付き日時を優先する
- 直上の時刻が見えにくい場合も、別のログの時刻や背景の時計で代用しない
- 正しい秒付き時刻を確認できない場合はtimestampを空文字にする

重要:
- 「初めて占領しました。」も占領ログとして抽出する
- 「激しい戦闘の末、〜を占領しました。」も占領ログとして抽出する
- 画像に占領ログが0件なら records は空配列
- 1枚に複数件あれば全件を上から順に返す
- 「県城Lv4祖厲」と見える場合でも city_name は「祖厲」だけ
- 「王城Lv10成都」と見える場合でも city_name は「成都」だけ
- 推測で存在しないログを作らない
- is_completeがfalseになるような不完全ログは、最初からrecordsへ入れない
- JSON以外の説明文を返さない

出力形式:
{"records":[{"timestamp":"2026/07/29 23:40:26","guild_name":"蜀国侍天之剣軍団","city_name":"墊江","coordinate":"266,684","is_complete":true}]}
"""

RECOVERY_PROMPT = r"""
この画像から「占領しました。」または「初めて占領しました。」を含む占領ログだけを上から順に抽出してください。
特に時刻を厳密に再確認してください。

時刻は、各占領吹き出しのすぐ上・中央付近にある白文字の日時だけを使ってください。
必ず秒まである HH:MM:SS を含む時刻だけが有効です。
半透明チャットの背後や左端に見える YYYY-MM-DD HH:MM の時計・透かしは絶対に使わないでください。
別の吹き出しの時刻を流用しないでください。
秒付き時刻が確認できない場合はtimestampを空文字にしてください。

city_nameは城名のみ、coordinateはX,Y、guild_nameは国名と軍団を含む見たままの文字です。
画像端で本文が本当に切れているログは返さないでください。
ただし、吹き出し内の正常な折り返しにより「占領しまし」と次行の「た。」に分かれている場合は、
最後の「た。」まで画像内に見えていれば完全な占領ログとして返してください。
見切れた内容を推測・補完しないでください。is_completeは完全に見えるログだけtrueです。
JSON以外は返さないでください。
出力形式:
{"records":[{"timestamp":"2026/07/30 21:09:32","guild_name":"蜀国倚天之剣軍団","city_name":"成都","coordinate":"83,656","is_complete":true}]}
"""


class GeminiImageAnalyzer:
    def __init__(self, api_key: str, model: str):
        self.client = genai.Client(api_key=api_key)
        self.model = model
        self.last_raw_response = ""

    def analyze(self, image_bytes: bytes, mime_type: str) -> list[OccupationRaw]:
        first_text = self._request(PROMPT, image_bytes, mime_type)
        first_records = self._records_from_text(first_text)

        # Version3.2:
        # 透かし等の「YYYY-MM-DD HH:MM」を拾い、秒が欠けた場合だけ再解析する。
        invalid_indexes = [
            index for index, record in enumerate(first_records)
            if not self._has_seconds(record.timestamp)
        ]
        if not invalid_indexes:
            self.last_raw_response = first_text
            return first_records

        logger.warning(
            "秒なし・不正時刻を%d件検出したため、吹き出し直上の時刻を再確認します",
            len(invalid_indexes),
        )
        recovery_text = self._request(RECOVERY_PROMPT, image_bytes, mime_type)
        recovery_records = self._records_from_text(recovery_text)
        merged = self._merge_recovery(first_records, recovery_records)
        self.last_raw_response = (
            "=== 初回解析 ===\n" + first_text
            + "\n\n=== 時刻再確認 ===\n" + recovery_text
        )
        return merged

    def _request(self, prompt: str, image_bytes: bytes, mime_type: str) -> str:
        response = self.client.models.generate_content(
            model=self.model,
            contents=[
                prompt,
                types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            ],
            config=types.GenerateContentConfig(
                temperature=0,
                response_mime_type="application/json",
            ),
        )
        return (response.text or "").strip()

    def _records_from_text(self, text: str) -> list[OccupationRaw]:
        payload = self._parse_json(text)
        records = payload.get("records", []) if isinstance(payload, dict) else []
        result: list[OccupationRaw] = []
        for item in records:
            if not isinstance(item, dict):
                continue
            timestamp = str(item.get("timestamp", "")).strip()
            guild = str(item.get("guild_name", "")).strip()
            city = str(item.get("city_name", "")).strip()
            coordinate = str(item.get("coordinate", "")).strip().replace("，", ",").replace(".", ",")
            complete_value = item.get("is_complete", True)
            is_complete = complete_value is True or str(complete_value).strip().lower() == "true"
            if not is_complete:
                logger.warning(
                    "⚠️ 見切れ・不完全OCRのため解析段階で除外: 時刻=%s / 城=%s / 座標=%s",
                    timestamp, city, coordinate,
                )
                continue
            if guild and city:
                result.append(OccupationRaw(timestamp, guild, city, coordinate, True))
        return result

    @staticmethod
    def _has_seconds(timestamp: str) -> bool:
        return bool(re.search(r"(?<!\d)\d{1,2}:\d{2}:\d{2}(?!\d)", timestamp or ""))

    @classmethod
    def _merge_recovery(
        cls,
        original: list[OccupationRaw],
        recovery: list[OccupationRaw],
    ) -> list[OccupationRaw]:
        """城名・座標を手掛かりに、再確認で得た秒付き時刻だけを安全に反映する。"""
        used: set[int] = set()
        merged: list[OccupationRaw] = []

        for record in original:
            if cls._has_seconds(record.timestamp):
                merged.append(record)
                continue

            best_index = None
            for index, candidate in enumerate(recovery):
                if index in used or not cls._has_seconds(candidate.timestamp):
                    continue
                same_coordinate = (
                    record.coordinate
                    and candidate.coordinate
                    and record.coordinate == candidate.coordinate
                )
                same_city = cls._normalize_key(record.city_name) == cls._normalize_key(candidate.city_name)
                same_guild = cls._normalize_key(record.guild_name) == cls._normalize_key(candidate.guild_name)
                if same_coordinate or (same_city and same_guild):
                    best_index = index
                    break

            if best_index is None:
                merged.append(record)
                continue

            candidate = recovery[best_index]
            used.add(best_index)
            logger.info(
                "🟡 時刻自動補正: %s / %s %s → %s",
                record.timestamp,
                record.city_name,
                record.coordinate,
                candidate.timestamp,
            )
            merged.append(
                OccupationRaw(
                    candidate.timestamp,
                    record.guild_name or candidate.guild_name,
                    record.city_name or candidate.city_name,
                    record.coordinate or candidate.coordinate,
                    record.is_complete and candidate.is_complete,
                )
            )
        return merged

    @staticmethod
    def _normalize_key(value: str) -> str:
        return re.sub(r"\s+", "", value or "").replace("國", "国")

    @staticmethod
    def _parse_json(text: str) -> dict:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if not match:
                logger.warning("Gemini応答にJSONがありません: %s", text[:300])
                return {"records": []}
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                logger.warning("Gemini応答JSONの解析に失敗: %s", text[:300])
                return {"records": []}
