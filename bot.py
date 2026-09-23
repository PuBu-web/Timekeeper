from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
import os
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import discord

import config
from image_analyzer import GeminiImageAnalyzer
from masters import CityMaster, GuildMaster, normalize_text
from storage import Storage
from time_utils import parse_timestamp

config.LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(config.LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("siege_timekeeper_v411_unconfirmed_notice")


# Renderのポートスキャンを通過させるためのダミーWebサーバー
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        # アクセスログでコンソールが埋まらないよう抑制
        return


def start_dummy_web_server() -> None:
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    logger.info("🌐 Render用ダミーWebサーバー起動: ポート %d", port)
    server.serve_forever()


# Windowsのコンソール右上「×」を検知するため、コールバックを保持する。
_WINDOWS_CONSOLE_HANDLER = None


def install_windows_close_handler(bot: "SiegeTimekeeperBot", loop: asyncio.AbstractEventLoop) -> None:
    """Windowsの×ボタン・ログオフ・シャットダウン時に安全終了を試みる。"""
    global _WINDOWS_CONSOLE_HANDLER
    if os.name != "nt":
        return

    import ctypes

    CTRL_CLOSE_EVENT = 2
    CTRL_LOGOFF_EVENT = 5
    CTRL_SHUTDOWN_EVENT = 6
    handled_events = {CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT}
    handler_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)

    @handler_type
    def console_handler(ctrl_type: int) -> bool:
        if ctrl_type not in handled_events:
            return False

        logger.info("🔴 Windowsの終了操作を検知: type=%s", ctrl_type)
        try:
            future = asyncio.run_coroutine_threadsafe(bot.close(), loop)
            # Windowsがプロセスを閉じる前に、Discord通知の送信を待つ。
            future.result(timeout=4.0)
        except Exception:
            logger.exception("Windows終了時の安全終了処理に失敗")
        return True

    if not ctypes.windll.kernel32.SetConsoleCtrlHandler(console_handler, True):
        logger.warning("🟡 Windowsの×ボタン終了検知を登録できませんでした")
        return

    _WINDOWS_CONSOLE_HANDLER = console_handler
    logger.info("🟢 Windowsの×ボタン安全終了を有効化")


def validate_config() -> None:
    missing = []
    if not config.DISCORD_TOKEN:
        missing.append("DISCORD_TOKEN")
    if not config.GEMINI_API_KEY:
        missing.append("GEMINI_API_KEY")
    if not config.IMAGE_INPUT_CHANNEL_ID:
        missing.append("IMAGE_INPUT_CHANNEL_ID")
    if not config.TIMEKEEPER_CHANNEL_ID:
        missing.append("TIMEKEEPER_CHANNEL_ID")
    if missing:
        raise RuntimeError("設定が不足しています: " + ", ".join(missing))
    if not config.MASTER_XLSX_PATH.exists():
        raise RuntimeError(f"設定Excelが見つかりません: {config.MASTER_XLSX_PATH}")


class SiegeTimekeeperBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.city_master = CityMaster(config.MASTER_XLSX_PATH)
        self.guild_master = GuildMaster(config.MASTER_XLSX_PATH)
        self.storage = Storage(config.DB_PATH)
        self.analyzer = GeminiImageAnalyzer(config.GEMINI_API_KEY, config.GEMINI_MODEL)
        self.processing_hashes: set[str] = set()
        self.startup_notified = False
        self.shutdown_notified = False

    async def on_ready(self) -> None:
        self.storage.cleanup(config.OCCUPATION_DEDUPE_HOURS, config.IMAGE_HASH_RETENTION_DAYS)
        logger.info("=" * 62)
        logger.info("攻城タイムキーパー Version4.1.1 OCR未確定通知対応版 起動完了: %s", self.user)
        logger.info("設定Excel: OK / 城データ=%d件 / 軍団データ=%d件", self.city_master.count, self.guild_master.count)
        logger.info("画像取込=%s / タイムキーパー=%s / DEBUG=%s", config.IMAGE_INPUT_CHANNEL_ID, config.TIMEKEEPER_CHANNEL_ID, "ON" if config.DEBUG_MODE else "OFF")
        logger.info("=" * 62)

        # Discordの再接続時に何度も投稿されないよう、1回の起動につき1回だけ通知する。
        if not self.startup_notified:
            await self._send_startup_notifications()
            self.startup_notified = True

    async def on_message(self, message: discord.Message) -> None:
        try:
            if config.reload_excel_settings():
                logger.info("🟢 設定.xlsxを再読み込み / 画像取込=%s / タイムキーパー=%s", config.IMAGE_INPUT_CHANNEL_ID, config.TIMEKEEPER_CHANNEL_ID)
        except Exception:
            logger.exception("🔴 設定.xlsxの再読み込みに失敗")
        if message.author.bot or message.channel.id != config.IMAGE_INPUT_CHANNEL_ID:
            return
        for attachment in message.attachments:
            suffix = Path(attachment.filename).suffix.lower()
            if suffix not in config.ALLOWED_EXTENSIONS:
                continue
            try:
                await self._handle_attachment(attachment, message.channel)
            except Exception:
                logger.exception("🔴 画像処理中にエラー: %s", attachment.filename)
                await self._send_log(f"❌ 画像処理エラー: `{attachment.filename}`\n詳細はPCのログを確認してください。")

    def _save_ocr_log(self, filename: str, raw_text: str) -> Path:
        config.LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        safe_stem = "".join(c if c.isalnum() or c in "-_" else "_" for c in Path(filename).stem)[:50]
        path = config.LOG_PATH.parent / f"OCR_{datetime.now():%Y%m%d_%H%M%S_%f}_{safe_stem}.txt"
        path.write_text(raw_text or "(空の応答)", encoding="utf-8")
        return path

    async def _handle_attachment(self, attachment: discord.Attachment, source_channel) -> None:
        progress_message = await source_channel.send("🔍 OCR解析中…")
        sent_count = 0

        if attachment.size and attachment.size > config.MAX_IMAGE_BYTES:
            logger.warning("🟡 画像サイズ超過のため無視: %s (%s bytes)", attachment.filename, attachment.size)
            await progress_message.edit(content="✅ 0件送信済み")
            return

        image_bytes = await attachment.read()
        image_hash = hashlib.sha256(image_bytes).hexdigest()
        if image_hash in self.processing_hashes or self.storage.is_image_processed(image_hash):
            logger.info("🟡 同一画像を静かに無視: %s", attachment.filename)
            await progress_message.edit(content="✅ 0件送信済み")
            return

        self.processing_hashes.add(image_hash)
        try:
            logger.info("画像受信: %s / OCR開始", attachment.filename)
            mime = attachment.content_type or mimetypes.guess_type(attachment.filename)[0] or "image/png"
            records = await asyncio.to_thread(self.analyzer.analyze, image_bytes, mime)
            ocr_log_path = self._save_ocr_log(attachment.filename, self.analyzer.last_raw_response)
            logger.info("OCR終了: 占領ログ=%d件 / OCR全文=%s", len(records), ocr_log_path.name)

            self.storage.mark_image_processed(image_hash)
            if not records:
                logger.info("占領ログなし: %s", attachment.filename)
                await progress_message.edit(content="✅ 0件送信済み")
                return

            output_lines: list[str] = []
            pending_saves: list[tuple[str, str, str, str, str]] = []

            for index, record in enumerate(records, start=1):
                logger.info("--- OCR結果 %d/%d ---", index, len(records))
                logger.info("時刻=%s / 城=%s / 座標=%s", record.timestamp, record.city_name, record.coordinate)
                logger.info("軍団OCR原文=%s", record.guild_name)

                occupied_at = parse_timestamp(record.timestamp)
                if occupied_at is None:
                    logger.warning("🟡 時刻解析失敗: %s", record.timestamp)
                    continue

                city = self.city_master.find(record.city_name, record.coordinate)
                if city is None:
                    reason = self.city_master.last_rejection_reason or "城データを安全に特定できません"
                    raw_city = normalize_text(record.city_name) or "城名不明"
                    raw_coord = normalize_text(record.coordinate).replace(".", ",")
                    coord_display = raw_coord if raw_coord else "座標不明"

                    guild_full = normalize_text(record.guild_name)
                    resolution = self.guild_master.resolve(guild_full, auto_add=False)
                    guild_short = resolution.short_name
                    guild_emoji = self.guild_master.emoji(resolution.full_name, resolution.faction)

                    warning_output = (
                        f"[{occupied_at:%H:%M:%S}] "
                        f"⚠️OCR未確定【{raw_city}】 {coord_display} "
                        f"{guild_emoji}{guild_short}"
                    )

                    warning_key = hashlib.sha256(
                        f"UNCONFIRMED|{occupied_at:%Y-%m-%d %H:%M:%S}|{raw_city}|{raw_coord}|{guild_full}".encode("utf-8")
                    ).hexdigest()

                    if self.storage.is_occupation_duplicate(warning_key, config.OCCUPATION_DEDUPE_HOURS):
                        logger.info("🟡 同一OCR未確定通知を静かに無視: %s", warning_output)
                        continue

                    logger.warning(
                        "⚠️ OCR未確定として通知: 城=%s 座標=%s / 理由=%s",
                        raw_city, raw_coord or "座標不明", reason,
                    )
                    logger.warning("   タイムキーパーへ警告送信。城名は設定.xlsxへ学習しません")
                    output_lines.append(warning_output)
                    pending_saves.append((
                        warning_key, occupied_at.isoformat(timespec="seconds"),
                        f"OCR未確定:{raw_city}", guild_full, warning_output,
                    ))
                    await self._send_log(
                        f"⚠️ OCR未確定としてタイムキーパーへ送信: `{raw_city}` / `{coord_display}`\n"
                        f"理由: {reason}"
                    )
                    continue

                raw_coord = normalize_text(record.coordinate).replace(".", ",")
                raw_city = normalize_text(record.city_name)
                if raw_city != city.name or raw_coord != city.coordinate:
                    logger.info(
                        "🟡 城データ自動補正: %s %s → %s %s",
                        raw_city, raw_coord, city.name, city.coordinate,
                    )
                else:
                    logger.info("🟢 城マスター一致: %s %s", city.name, city.coordinate)

                if self.city_master.last_learning:
                    wrong, correct = self.city_master.last_learning
                    logger.info("🧠 城名を学習してExcelへ保存: %s → %s", wrong, correct)
                elif self.city_master.last_learning_error:
                    logger.warning("🟡 城名の学習保存に失敗: %s", self.city_master.last_learning_error)
                    logger.warning("   設定.xlsxをExcelで開いている場合は閉じてください")

                guild_full = normalize_text(record.guild_name)
                resolution = self.guild_master.resolve(guild_full, auto_add=True)
                guild_short = resolution.short_name
                if resolution.registered:
                    logger.info("🟢 軍団マスター一致: %s → %s", guild_full, guild_short)
                    if resolution.learned:
                        logger.info("🧠 軍団名を学習してExcelへ保存: %s → %s", guild_full, resolution.full_name)
                    elif resolution.learning_error:
                        logger.warning("🟡 軍団名の学習保存に失敗: %s", resolution.learning_error)
                        logger.warning("   設定.xlsxをExcelで開いている場合は閉じてください")
                elif resolution.auto_added:
                    logger.warning("🟡 未登録軍団をExcelへ仮登録: %s → %s", guild_full, guild_short)
                    logger.warning("   data\\設定.xlsx の『軍団データ』で登録名を修正してください")
                elif resolution.save_error:
                    logger.warning("🟡 未登録軍団: %s → %s / Excel追記失敗=%s", guild_full, guild_short, resolution.save_error)
                    logger.warning("   設定.xlsxをExcelで開いている場合は閉じてから再テストしてください")
                else:
                    logger.warning("🟡 未登録軍団: %s → %s", guild_full, guild_short)

                guild_emoji = self.guild_master.emoji(resolution.full_name, city.faction)
                next_time = occupied_at + timedelta(minutes=config.COOLDOWN_MINUTES)
                output = (
                    f"[{next_time:%H:%M:%S}]"
                    f"【{city.emoji}{city.county}】"
                    f"{city.name}{city.coordinate} "
                    f"{guild_emoji}{guild_short}"
                )

                occupation_key = hashlib.sha256(
                    f"{occupied_at:%Y-%m-%d %H:%M:%S}|{city.name}|{guild_full}".encode("utf-8")
                ).hexdigest()
                if self.storage.is_occupation_duplicate(occupation_key, config.OCCUPATION_DEDUPE_HOURS):
                    logger.info("🟡 同一占領を静かに無視: %s", output)
                    continue

                output_lines.append(output)
                pending_saves.append((occupation_key, occupied_at.isoformat(timespec="seconds"), city.name, guild_full, output))

            if not output_lines:
                await progress_message.edit(content="✅ 0件送信済み")
                return

            channel = await self._resolve_channel(config.TIMEKEEPER_CHANNEL_ID)
            if channel is None:
                raise RuntimeError("タイムキーパーチャンネルを取得できません")

            chunks: list[str] = []
            current = ""
            for line in output_lines:
                candidate = line if not current else current + "\n" + line
                if len(candidate) > 1900:
                    chunks.append(current)
                    current = line
                else:
                    current = candidate
            if current:
                chunks.append(current)

            for chunk in chunks:
                await channel.send(chunk)

            for args in pending_saves:
                self.storage.save_occupation(*args)
            sent_count = len(output_lines)
            logger.info("📨 タイムキーパーへ送信完了: %d件", sent_count)
            await progress_message.edit(content=f"✅ {sent_count}件送信済み")
        except Exception:
            try:
                await progress_message.edit(content=f"✅ {sent_count}件送信済み")
            except discord.HTTPException:
                logger.exception("進捗メッセージ更新失敗")
            raise
        finally:
            self.processing_hashes.discard(image_hash)

    async def _send_startup_notifications(self) -> None:
        notifications = [
            (config.IMAGE_INPUT_CHANNEL_ID, "🚀  BOT起動しました\n占領ログの画像を受け付けています。"),
            (config.TIMEKEEPER_CHANNEL_ID, "🚀  BOT起動しました\nタイムキーパー稼働中です。"),
        ]
        for channel_id, text in notifications:
            channel = await self._resolve_channel(channel_id)
            if channel is None:
                logger.warning("🟡 起動通知先を取得できません: %s", channel_id)
                continue
            try:
                await channel.send(text)
                logger.info("🚀  起動通知を送信: %s", channel_id)
            except discord.HTTPException:
                logger.exception("起動通知の送信に失敗: %s", channel_id)

    async def _send_shutdown_notifications(self) -> None:
        notifications = [
            (config.IMAGE_INPUT_CHANNEL_ID, "🛑 BOT終了しました\n占領ログ画像の受付を停止しました。"),
            (config.TIMEKEEPER_CHANNEL_ID, "🛑 BOT終了しました\nタイムキーパーを停止しました。"),
        ]
        for channel_id, text in notifications:
            channel = await self._resolve_channel(channel_id)
            if channel is None:
                logger.warning("🟡 終了通知先を取得できません: %s", channel_id)
                continue
            try:
                await channel.send(text)
                logger.info("🛑 終了通知を送信: %s", channel_id)
            except discord.HTTPException:
                logger.exception("終了通知の送信に失敗: %s", channel_id)

    async def close(self) -> None:
        if self.is_ready() and not self.shutdown_notified:
            self.shutdown_notified = True
            await self._send_shutdown_notifications()
        await super().close()

    async def _resolve_channel(self, channel_id: int):
        channel = self.get_channel(channel_id)
        if channel is not None:
            return channel
        try:
            return await self.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            logger.exception("Discordチャンネル取得失敗: %s", channel_id)
            return None

    async def _send_log(self, text: str) -> None:
        if not config.LOG_CHANNEL_ID:
            return
        channel = await self._resolve_channel(config.LOG_CHANNEL_ID)
        if channel is not None:
            try:
                await channel.send(text)
            except discord.HTTPException:
                logger.exception("Discordログ送信失敗")


async def run_bot() -> None:
    bot = SiegeTimekeeperBot()
    loop = asyncio.get_running_loop()
    install_windows_close_handler(bot, loop)
    try:
        await bot.start(config.DISCORD_TOKEN)
    finally:
        if not bot.is_closed():
            await bot.close()


def main() -> None:
    validate_config()
    
　　from keep_alive import keep_alive
　　keep_alive()
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        logger.info("Ctrl+Cによる終了を受け付けました")


if __name__ == "__main__":
    main()
