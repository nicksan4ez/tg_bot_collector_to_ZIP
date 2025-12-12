from __future__ import annotations

import asyncio
import mimetypes
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.types.input_file import FSInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .logging_setup import setup_logging
from .settings import settings

logger = setup_logging(settings)


class AuthorizedMessageFilter(BaseFilter):
    def __init__(self, allowed_user_ids: Set[int]):
        self.allowed_user_ids = allowed_user_ids

    async def __call__(self, message: Message) -> bool:
        user = message.from_user
        if user and user.id in self.allowed_user_ids:
            return True
        if user:
            logger.info("Unauthorized message from user_id=%s ignored", user.id)
        else:
            logger.info("Unauthorized message without user information ignored")
        return False


class AuthorizedCallbackFilter(BaseFilter):
    def __init__(self, allowed_user_ids: Set[int]):
        self.allowed_user_ids = allowed_user_ids

    async def __call__(self, callback: CallbackQuery) -> bool:
        user = callback.from_user
        if user and user.id in self.allowed_user_ids:
            return True
        if user:
            logger.info("Unauthorized callback from user_id=%s ignored", user.id)
        else:
            logger.info("Unauthorized callback without user information ignored")
        return False


CONFIRM_CALLBACK_DATA = "archive:confirm"
IGNORE_CALLBACK_DATA = "archive:ignore"

INVALID_FILENAME_CHARS = '<>:"/\\|?*'


@dataclass(slots=True)
class VideoPayload:
    file_id: str
    caption: Optional[str]
    mime_type: Optional[str]
    original_file_name: Optional[str]
    message_id: int
    chat_id: int


@dataclass(slots=True)
class SessionState:
    videos: List[VideoPayload] = field(default_factory=list)
    prompt_task: Optional[asyncio.Task] = None
    prompt_message_id: Optional[int] = None

    def cancel_prompt(self) -> None:
        if self.prompt_task and not self.prompt_task.done():
            self.prompt_task.cancel()
        self.prompt_task = None

    def reset(self) -> None:
        self.cancel_prompt()
        self.videos.clear()
        self.prompt_message_id = None


router = Router()
router.message.filter(AuthorizedMessageFilter(set(settings.allowed_user_ids)))
router.callback_query.filter(AuthorizedCallbackFilter(set(settings.allowed_user_ids)))

_sessions: Dict[int, SessionState] = {}


def _get_or_create_session(user_id: int) -> SessionState:
    state = _sessions.get(user_id)
    if state is None:
        state = SessionState()
        _sessions[user_id] = state
    return state


def _clear_session(user_id: int) -> None:
    state = _sessions.pop(user_id, None)
    if state:
        state.reset()


def _build_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Да", callback_data=CONFIRM_CALLBACK_DATA),
        InlineKeyboardButton(text="❌ Игнорировать", callback_data=IGNORE_CALLBACK_DATA),
    )
    return builder.as_markup()


def _sanitize_filename(name: str) -> str:
    sanitized = []
    for char in name:
        if char == ":":
            sanitized.append(" -")
            continue
        if char in INVALID_FILENAME_CHARS:
            sanitized.append("_")
        else:
            sanitized.append(char)
    candidate = "".join(sanitized).strip()
    return candidate or "video"


def _resolve_extension(payload: VideoPayload) -> str:
    if payload.original_file_name:
        suffix = Path(payload.original_file_name).suffix
        if suffix:
            return suffix
    if payload.mime_type:
        guessed = mimetypes.guess_extension(payload.mime_type, strict=False)
        if guessed:
            return guessed
    return ".mp4"




def _split_archive_into_volumes(archive_path: Path, max_bytes: int) -> List[Path]:
    parts: List[Path] = []
    if max_bytes <= 0:
        return [archive_path]
    with archive_path.open("rb") as source:
        index = 1
        while True:
            chunk = source.read(max_bytes)
            if not chunk:
                break
            part_name = f"{archive_path.name}.{index:03d}"
            part_path = archive_path.with_name(part_name)
            with part_path.open("wb") as target:
                target.write(chunk)
            parts.append(part_path)
            index += 1
    if parts:
        archive_path.unlink(missing_ok=True)
    return parts or [archive_path]








async def _schedule_prompt(message: Message, state: SessionState) -> None:
    try:
        await asyncio.sleep(settings.archive_build_delay)
    except asyncio.CancelledError:
        logger.debug(
            "Prompt task for user_id=%s cancelled",
            message.from_user.id if message.from_user else "?",
        )
        return

    if not state.videos:
        return

    sent = await message.answer("Собрать в архив?", reply_markup=_build_keyboard())
    state.prompt_message_id = sent.message_id
    state.prompt_task = None
    logger.debug(
        "Prompt message sent to user_id=%s",
        message.from_user.id if message.from_user else "?",
    )


@router.message(F.video)
async def handle_video(message: Message) -> None:
    user_id = message.from_user.id  # Authorized filter guarantees presence
    state = _get_or_create_session(user_id)

    payload = VideoPayload(
        file_id=message.video.file_id,
        caption=message.caption,
        mime_type=message.video.mime_type,
        original_file_name=message.video.file_name,
        message_id=message.message_id,
        chat_id=message.chat.id,
    )
    state.videos.append(payload)
    logger.info(
        "Queued video file_id=%s for user_id=%s (caption=%s)",
        payload.file_id,
        user_id,
        payload.caption,
    )

    state.cancel_prompt()

    if state.prompt_message_id:
        try:
            await message.bot.delete_message(chat_id=message.chat.id, message_id=state.prompt_message_id)
            logger.debug("Deleted previous prompt for user_id=%s", user_id)
        except TelegramBadRequest:
            logger.debug("Failed to delete previous prompt for user_id=%s", user_id)
        state.prompt_message_id = None

    state.prompt_task = asyncio.create_task(_schedule_prompt(message, state))


async def _delete_prompt_message(callback: CallbackQuery, state: SessionState) -> None:
    if not state.prompt_message_id or not callback.message:
        return
    bot = callback.bot
    try:
        await bot.delete_message(callback.message.chat.id, state.prompt_message_id)
    except TelegramBadRequest:
        logger.debug(
            "Prompt message already deleted for user_id=%s",
            callback.from_user.id if callback.from_user else "?",
        )
    finally:
        state.prompt_message_id = None


def _build_target_filename(base_name: str, extension: str, existing: Set[str]) -> str:
    sanitized_base = _sanitize_filename(base_name)
    candidate = f"{sanitized_base}{extension}"
    suffix = 1
    while candidate in existing:
        candidate = f"{sanitized_base}_{suffix:02d}{extension}"
        suffix += 1
    existing.add(candidate)
    return candidate


async def _download_video(bot: Bot, payload: VideoPayload, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.wait_for(
        bot.download(payload.file_id, destination=destination),
        timeout=settings.download_timeout,
    )


async def _process_archive(callback: CallbackQuery, state: SessionState) -> None:
    message = callback.message
    if not message:
        logger.warning(
            "Callback without message context for user_id=%s",
            callback.from_user.id if callback.from_user else "?",
        )
        return

    user_id = callback.from_user.id if callback.from_user else 0
    work_dir = Path(tempfile.mkdtemp(dir=settings.temp_root))
    downloaded_files: List[Tuple[Path, str]] = []
    failed_payloads: List[Tuple[VideoPayload, str]] = []
    existing_names: Set[str] = set()
    try:
        for index, payload in enumerate(state.videos, start=1):
            caption = payload.caption or f"video_{index:02d}"
            extension = _resolve_extension(payload)
            target_filename = _build_target_filename(caption, extension, existing_names)
            target_path = work_dir / target_filename
            try:
                await _download_video(callback.bot, payload, target_path)
                downloaded_files.append((target_path, caption))
                logger.info(
                    "Downloaded video for user_id=%s as %s",
                    user_id,
                    target_filename,
                )
            except Exception:  # noqa: BLE001 - log and continue with next file
                logger.exception(
                    "Failed to download video file_id=%s for user_id=%s",
                    payload.file_id,
                    user_id,
                )
                failed_payloads.append((payload, caption))

        if failed_payloads:
            await message.answer("Это не скачал, отправь отдельно:")
            for failed_payload, caption in failed_payloads:
                try:
                    await message.bot.send_video(
                        chat_id=failed_payload.chat_id,
                        video=failed_payload.file_id,
                        caption=failed_payload.caption,
                    )
                except Exception:  # noqa: BLE001 - log and continue with next file
                    logger.exception(
                        "Failed to resend undownloaded video file_id=%s for user_id=%s",
                        failed_payload.file_id,
                        user_id,
                    )

        if not downloaded_files:
            await message.answer(
                "❌Не удалось скачать ни один файл. Попробуйте повторно отправить сообщения."
            )
            return

        archive_path = work_dir / settings.archive_name
        with zipfile.ZipFile(archive_path, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
            for file_path, _ in downloaded_files:
                archive.write(file_path, arcname=file_path.name)
        logger.info("Created archive %s for user_id=%s", archive_path, user_id)

        limit = settings.archive_size_limit_bytes
        archive_size = archive_path.stat().st_size
        if limit <= 0 or archive_size <= limit:
            part_paths = [archive_path]
        else:
            part_paths = _split_archive_into_volumes(archive_path, limit)
            logger.info(
                "Archive for user_id=%s split into %s volumes (limit=%s bytes)",
                user_id,
                len(part_paths),
                limit,
            )

        total_parts = len(part_paths)
        for index, part_path in enumerate(part_paths, start=1):
            caption_text = "✅Готово"
            if total_parts > 1:
                caption_text = (
                    f"✅Готово. Архив том {index}/{total_parts}. Загрузите все тома перед распаковкой."
                )
            await message.answer_document(
                document=FSInputFile(part_path),
                caption=caption_text,
            )

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        logger.debug("Cleaned work directory %s for user_id=%s", work_dir, user_id)
@router.callback_query(F.data == CONFIRM_CALLBACK_DATA)
async def on_confirm(callback: CallbackQuery) -> None:
    if not callback.from_user:
        await callback.answer()
        return

    user_id = callback.from_user.id
    state = _sessions.get(user_id)
    if not state or not state.videos:
        await callback.answer("Нет файлов для обработки", show_alert=True)
        if state:
            await _delete_prompt_message(callback, state)
            _clear_session(user_id)
        return

    await callback.answer()
    await _delete_prompt_message(callback, state)
    state.cancel_prompt()

    await _process_archive(callback, state)
    _clear_session(user_id)


@router.callback_query(F.data == IGNORE_CALLBACK_DATA)
async def on_ignore(callback: CallbackQuery) -> None:
    await callback.answer()
    if not callback.from_user:
        return

    user_id = callback.from_user.id
    state = _sessions.get(user_id)
    if state:
        await _delete_prompt_message(callback, state)
    _clear_session(user_id)
    logger.info("User_id=%s ignored queued videos", user_id)


async def run() -> None:
    logger.info("Starting bot")
    bot = Bot(token=settings.bot_token, parse_mode=ParseMode.HTML)
    dp = Dispatcher()
    dp.include_router(router)

    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")
