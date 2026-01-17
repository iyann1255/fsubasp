from __future__ import annotations

import logging
import random
from uuid import uuid4

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import load_config
from fsub import build_join_keyboard, is_user_joined_all
from shortlink import gen_code
from storage import FileRecord, build_storage

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("fsub-modern")

CFG = load_config()
STORE = build_storage(CFG.storage_backend, CFG.mongo_uri, CFG.mongo_db)

CB_DONE = "fsub_done"


def _mention_html(user) -> str:
    name = (user.first_name or "bro").replace("<", "").replace(">", "")
    return f"<a href='tg://user?id={user.id}'>{name}</a>"


def _is_admin(user_id: int) -> bool:
    return user_id in CFG.admins


def _pick_db_target() -> int:
    # sebar random biar gak numpuk di 1 channel
    return random.choice(CFG.db_targets)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if not u or not update.message:
        return
    text = CFG.start_message.format(mention=_mention_html(u))
    await update.message.reply_html(text, disable_web_page_preview=True)


async def gate_or_send_by_chat(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, file_id: str) -> None:
    ok = await is_user_joined_all(context, user_id, CFG.force_sub_targets)
    if not ok:
        kb = await build_join_keyboard(
            context=context,
            targets=CFG.force_sub_targets,
            buttons_per_row=CFG.buttons_per_row,
            join_text=CFG.join_text,
            done_callback_data=f"{CB_DONE}:{file_id}",
            max_buttons=CFG.max_join_buttons,
        )
        await context.bot.send_message(
            chat_id=chat_id,
            text=CFG.force_sub_message,
            reply_markup=kb,
            disable_web_page_preview=True,
            parse_mode="HTML",
        )
        return

    rec = STORE.get(file_id)
    if not rec:
        await context.bot.send_message(chat_id=chat_id, text="File tidak ditemukan / sudah dihapus dari DB.")
        return

    try:
        await context.bot.copy_message(
            chat_id=chat_id,
            from_chat_id=rec.db_chat_id,
            message_id=rec.db_message_id,
        )
    except Exception as e:
        log.exception("copy_message failed: %s", e)
        await context.bot.send_message(
            chat_id=chat_id,
            text="Gagal ambil file dari DB. Pastikan bot punya akses read/copy di DB target.",
        )


async def deep_link_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return

    args = context.args
    if not args:
        await start_cmd(update, context)
        return

    code = args[0].strip()
    file_id = STORE.get_file_id_by_code(code)
    if not file_id:
        await update.message.reply_text("Link invalid / sudah tidak berlaku.")
        return

    await gate_or_send_by_chat(context, update.message.chat_id, update.effective_user.id, file_id)


async def done_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not q.from_user or not q.message:
        return

    data = (q.data or "")
    if not data.startswith(f"{CB_DONE}:"):
        await q.answer()
        return

    file_id = data.split(":", 1)[1].strip()

    ok = await is_user_joined_all(context, q.from_user.id, CFG.force_sub_targets)
    if not ok:
        await q.answer("Masih belum join semua ya.", show_alert=True)
        return

    await q.answer()

    # optional: hapus gate message biar bersih
    try:
        await q.message.delete()
    except Exception:
        pass

    await gate_or_send_by_chat(context, q.message.chat_id, q.from_user.id, file_id)


async def save_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    u = update.effective_user
    if not msg or not u:
        return

    # Deteksi jenis file
    kind = None
    if msg.document:
        kind = "document"
    elif msg.video:
        kind = "video"
    elif msg.photo:
        kind = "photo"
    elif msg.audio:
        kind = "audio"
    elif msg.voice:
        kind = "voice"
    else:
        return

    # USER mode: wajib FSUB dulu sebelum boleh upload (admin bypass)
    if (not _is_admin(u.id)) and CFG.force_sub_targets:
        ok = await is_user_joined_all(context, u.id, CFG.force_sub_targets)
        if not ok:
            kb = await build_join_keyboard(
                context=context,
                targets=CFG.force_sub_targets,
                buttons_per_row=CFG.buttons_per_row,
                join_text=CFG.join_text,
                done_callback_data="noop",
                max_buttons=CFG.max_join_buttons,
            )
            await msg.reply_html(CFG.force_sub_message, reply_markup=kb, disable_web_page_preview=True)
            return

    # copy ke salah satu DB target
    db_chat_id = _pick_db_target()
    try:
        copied = await context.bot.copy_message(
            chat_id=db_chat_id,
            from_chat_id=msg.chat_id,
            message_id=msg.message_id,
        )
    except Exception as e:
        log.exception("copy to db target failed: %s", e)
        await msg.reply_text("Gagal simpan ke DB target. Pastikan bot punya akses kirim pesan/admin di target.")
        return

    file_id = str(uuid4())
    STORE.upsert(
        FileRecord(
            file_id=file_id,
            db_chat_id=db_chat_id,
            db_message_id=copied.message_id,
            kind=kind,
            caption=msg.caption_html if msg.caption_html else None,
        )
    )

    me = await context.bot.get_me()
    if not me.username:
        await msg.reply_text("Bot belum punya username. Set dulu di @BotFather biar link /start bisa dipakai.")
        return

    # generate code pendek unik
    code = None
    for _ in range(40):
        c = gen_code(10)
        if not STORE.get_file_id_by_code(c):
            code = c
            break

    if not code:
        await msg.reply_text("Gagal generate code unik. Coba ulang.")
        return

    STORE.save_link(code, file_id)

    link = f"https://t.me/{me.username}?start={code}"
    await msg.reply_html(
        f"<b>Saved.</b>\n\nLink:\n<code>{link}</code>",
        disable_web_page_preview=True,
    )


def main() -> None:
    app: Application = ApplicationBuilder().token(CFG.bot_token).build()

    app.add_handler(CommandHandler("start", deep_link_start))
    app.add_handler(CallbackQueryHandler(done_cb, pattern=r"^fsub_done:"))

    app.add_handler(
        MessageHandler(
            (filters.Document.ALL | filters.VIDEO | filters.PHOTO | filters.AUDIO | filters.VOICE),
            save_file,
        )
    )

    log.info("Bot running...")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
