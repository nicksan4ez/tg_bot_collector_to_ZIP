#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import asyncio
import logging
import zipfile
import shutil
import time
from pathlib import Path
from typing import List

from dotenv import load_dotenv
from telegram import Update, InputFile
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ALLOWED_USERS = os.getenv("ALLOWED_USERS", "")
TMP_ROOT_ENV = os.getenv("TMP_ROOT", "tmp")
ZIP_NAME = os.getenv("ZIP_NAME", "Monitor.zip")
# support both ARCHIVE_DELAY and legacy DEBOUNCE_SECONDS env var
ARCHIVE_DELAY = float(os.getenv("ARCHIVE_DELAY", os.getenv("DEBOUNCE_SECONDS", "5")))

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN required in .env")

BASE_DIR = Path(__file__).parent.resolve()
TMP_ROOT = Path(TMP_ROOT_ENV)
if not TMP_ROOT.is_absolute():
    TMP_ROOT = (BASE_DIR / TMP_ROOT_ENV).resolve()

ALLOWED_USERS_SET = {int(x.strip()) for x in ALLOWED_USERS.split(",") if x.strip().isdigit()}

class UserState:
    def __init__(self, uid: int, chat_id: int):
        self.uid = uid
        self.chat_id = chat_id
        self.dirpath = TMP_ROOT / f"user_{uid}"
        self.dirpath.mkdir(parents=True, exist_ok=True)
        self.saved_files: List[Path] = []
        self.in_progress = 0
        self.last_ts = 0.0
        self.lock = asyncio.Lock()
        self.finalize_task: asyncio.Task | None = None

USER_STATES: dict[int, UserState] = {}

def _mime_to_ext(mime: str) -> str:
    mime = (mime or "").lower()
    if "mp4" in mime: return ".mp4"
    if "webm" in mime: return ".webm"
    if "ogg" in mime: return ".ogg"
    if "mpeg" in mime or "mp3" in mime: return ".mp3"
    if "jpeg" in mime or "jpg" in mime: return ".jpg"
    if "png" in mime: return ".png"
    return ""

def sanitize_preserve_visual(name: str) -> str:
    if name is None:
        return ""
    n = str(name)
    n = n.replace("\x00", "")
    n = n.replace("/", "∕").replace("\\", "∕")
    replacements = {":": "：", "*": "∗", "?": "？", '"': "＂", "<": "＜", ">": "＞", "|": "∣"}
    for k, v in replacements.items():
        n = n.replace(k, v)
    n = n.replace("\r", " ").replace("\n", " ")
    return n.strip()

def make_unique_filepath(dirpath: Path, desired_name: str) -> Path:
    desired = Path(desired_name)
    base = desired.stem
    ext = desired.suffix or ""
    candidate = dirpath / (base + ext)
    i = 1
    while candidate.exists():
        candidate = dirpath / f"{base} ({i}){ext}"
        i += 1
    return candidate

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return
    uid = update.effective_user.id
    chat_id = update.effective_chat.id if update.effective_chat else None
    if ALLOWED_USERS_SET and uid not in ALLOWED_USERS_SET:
        logger.info("User %s not allowed; ignoring", uid)
        return
    msg = update.message
    if not msg:
        return

    media_list = []
    if msg.video:
        media_list.append(msg.video)
    if msg.document:
        media_list.append(msg.document)
    if not media_list:
        return

    # get or create state
    state = USER_STATES.get(uid)
    if state is None:
        state = UserState(uid, chat_id)
        USER_STATES[uid] = state

    # download all items in this message
    for media in media_list:
        async with state.lock:
            state.in_progress += 1
        try:
            file_obj = await context.bot.get_file(media.file_id)

            # determine extension robustly
            ext = ""
            media_file_name = getattr(media, "file_name", None)
            if media_file_name:
                ext = Path(media_file_name).suffix
            # prefer file path from API
            if not ext and getattr(file_obj, "file_path", None):
                try:
                    ext = Path(file_obj.file_path).suffix
                except Exception:
                    ext = ""
            # fallback to mime
            if not ext:
                ext = _mime_to_ext(getattr(media, "mime_type", "") or "")

            raw_name = msg.caption or media_file_name or "file"
            safe_base = sanitize_preserve_visual(raw_name)
            # ensure we append extension if known and not already present
            if ext and not safe_base.lower().endswith(ext.lower()):
                safe_name = safe_base + ext
            else:
                safe_name = safe_base

            saved_path = make_unique_filepath(state.dirpath, safe_name)

            # download
            await file_obj.download_to_drive(custom_path=str(saved_path))

            # if the expected file wasn't created, try to find a candidate (temp .name/.part) and rename it
            if not saved_path.exists():
                now_ts = time.time()
                candidates = []
                for p in state.dirpath.iterdir():
                    if not p.is_file():
                        continue
                    # recent files only (last 60s) to avoid old leftovers
                    try:
                        mtime = p.stat().st_mtime
                    except Exception:
                        mtime = 0
                    if now_ts - mtime > 60:
                        continue
                    name = p.name
                    if name.startswith(saved_path.stem):
                        candidates.append(p)
                        continue
                    # some downloaders create names like 'saved_<file_id>'
                    if name.startswith('saved_'):
                        candidates.append(p)
                        continue
                    # include files containing the telegram file_id
                    fid = getattr(media, 'file_id', None)
                    if fid and fid in name:
                        candidates.append(p)
                        continue
                    # include files with the expected extension
                    if ext and p.suffix.lower() == ext.lower():
                        candidates.append(p)
                        continue
                if candidates:
                    # pick largest candidate
                    candidate = max(candidates, key=lambda p: p.stat().st_size)
                    try:
                        candidate.rename(saved_path)
                        logger.info("Renamed candidate %s -> %s", candidate, saved_path)
                    except Exception:
                        logger.exception("Failed to rename candidate %s to %s", candidate, saved_path)

            # final check: only append if file exists
            if saved_path.exists() and saved_path.is_file():
                # avoid adding helper files like *.name
                if not saved_path.name.endswith(".name") and not saved_path.name.endswith(".part"):
                    async with state.lock:
                        state.saved_files.append(saved_path)
                    logger.info("Saved file for user %s: %s", uid, saved_path)
                else:
                    logger.info("Skipped helper file for user %s: %s", uid, saved_path)
            else:
                logger.warning("File expected but not found after download for user %s: %s", uid, saved_path)

        except Exception as e:
            logger.exception("Error downloading media for user %s: %s", uid, e)
        finally:
            async with state.lock:
                state.in_progress -= 1

    # update timestamp and restart finalizer
    now = time.time()
    async with state.lock:
        state.last_ts = now
        if state.finalize_task and not state.finalize_task.done():
            try:
                state.finalize_task.cancel()
            except Exception:
                pass
        # pass chat_id so finalizer can send
        state.finalize_task = context.application.create_task(_finalize_after_delay(uid, now, state.chat_id, context))

async def _finalize_after_delay(uid: int, ts: float, chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    try:
        await asyncio.sleep(ARCHIVE_DELAY)
        state = USER_STATES.get(uid)
        if state is None:
            return
        async with state.lock:
            if state.last_ts != ts:
                return
        waited = 0.0
        while True:
            async with state.lock:
                in_prog = state.in_progress
            if in_prog == 0:
                break
            await asyncio.sleep(0.2)
            waited += 0.2
            if waited > 30.0:
                break
        async with state.lock:
            if state.last_ts != ts:
                return
            files = list(state.saved_files)
            dirpath = state.dirpath

        if not files:
            try:
                shutil.rmtree(dirpath, ignore_errors=True)
            except Exception:
                pass
            USER_STATES.pop(uid, None)
            return

        zip_path = dirpath / ZIP_NAME
        try:
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as zf:
                for f in files:
                    # include only the files we explicitly saved and that still exist
                    if f.exists() and f.is_file():
                        # skip helper files if any sneaked in
                        if f.name.endswith('.name') or f.name.endswith('.part'):
                            logger.info("Skipping helper file in zip: %s", f)
                            continue
                        zf.write(f, arcname=f.name)

            # send archive
            with zip_path.open("rb") as fh:
                await context.bot.send_document(chat_id=chat_id, document=InputFile(fh), filename=ZIP_NAME)
            logger.info("Sent archive to chat %s for user %s", chat_id, uid)
        except Exception as e:
            logger.exception("Failed to send archive for user %s: %s", uid, e)
        finally:
            try:
                shutil.rmtree(dirpath, ignore_errors=True)
            except Exception:
                logger.exception("Failed to remove tmp dir %s", dirpath)
            USER_STATES.pop(uid, None)
    except asyncio.CancelledError:
        return
    except Exception:
        logger.exception("Unexpected error in finalizer for user %s", uid)

def main():
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.Document.ALL | filters.VIDEO, handle_media))
    logger.info("Bot started. TMP_ROOT=%s ARCHIVE_DELAY=%s", TMP_ROOT, ARCHIVE_DELAY)
    app.run_polling()

if __name__ == "__main__":
    main()
