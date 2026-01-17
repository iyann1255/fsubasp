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
from fsub import build_join_keyboard, is_user_joined_all, visible_targets_for_user, split_target
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
    return random.choice(CFG.db_targets)


async def _send_gate(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE, file_id: str | None) -> None:
    """
    AUTO-ROTATE per file:
    - gate_key = file_id (atau "__none__")
    - kalau gate_key beda dari last_gate_key user → offset++ otomatis
    """
    gate_key = file_id or "__none__"
    st = STORE.get_user_state(user_id)

    # tracking: kalau user pindah ke gate file lain padahal belum join,
    # anggap dia "melewatin" batch yang sebelumnya ditawarin (skips++)
    if st.last_gate_key and st.last_gate_key != gate_key:
        prev_visible = visible_targets_for_user(
            CFG.force_sub_targets,
            user_id=user_id,
            offset=st.offset,
            k=CFG.max_join_buttons,
        )
        for raw in prev_visible:
            check_chat, _ = split_target(raw)
            STORE.inc_skip(str(check_chat), 1)

        st = STORE.bump_rotate(user_id)

    # set gate key terbaru
    if st.last_gate_key != gate_key:
        STORE.set_last_gate_key(user_id, gate_key)
        st = STORE.get_user_state(user_id)

    done_data = f"{CB_DONE}:{file_id}" if file_id else f"{CB_DONE}:__none__"

    kb = await build_join_keyboard(
        context=context,
        targets=CFG.force_sub_targets,
        user_id=user_id,
        offset=st.offset,
        buttons_per_row=CFG.buttons_per_row,
        join_text=CFG.join_text,
        done_callback_data=done_data,
        max_buttons=CFG.max_join_buttons,
    )

    await context.bot.send_message(
        chat_id=chat_id,
        text=CFG.force_sub_message,
        reply_markup=kb,
        disable_web_page_preview=True,
        parse_mode="HTML",
    )


async def _send_file(chat_id: int, context: ContextTypes.DEFAULT_TYPE, file_id: str) -> None:
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
        await context.bot.send_message(chat_id=chat_id, text="Gagal ambil file dari DB. Cek akses bot di DB target.")


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return

    u = update.effective_user
    args = context.args

    # no payload: tampilkan gate kalau ada fsub (serba button)
if not args:
    text = CFG.start_message.format(mention=_mention_html(u))
    await update.message.reply_html(
        text,
        disable_web_page_preview=True
    )
    return


    # deep-link redeem
    code = args[0].strip()
    file_id = STORE.get_file_id_by_code(code)
    if not file_id:
        await update.message.reply_text("Link invalid / sudah tidak berlaku.")
        return

    ok = await is_user_joined_all(context, u.id, CFG.force_sub_targets)
    if not ok:
        await _send_gate(update.message.chat_id, u.id, context, file_id=file_id)
        return

    await _send_file(update.message.chat_id, context, file_id)


async def done_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not q.from_user or not q.message:
        return

    data = (q.data or "")
    if not data.startswith(f"{CB_DONE}:"):
        await q.answer()
        return

    file_id = data.split(":", 1)[1].strip()
    if file_id == "__none__":
        file_id = None

    ok = await is_user_joined_all(context, q.from_user.id, CFG.force_sub_targets)
    if not ok:
        await q.answer("Masih belum join semua.", show_alert=True)
        return

    await q.answer()

    # clean gate message
    try:
        await q.message.delete()
    except Exception:
        pass

    # reset gate key biar next file bisa rotate normal
    STORE.set_last_gate_key(q.from_user.id, "")

    if file_id:
        await _send_file(q.message.chat_id, context, file_id)
    else:
        await context.bot.send_message(chat_id=q.message.chat_id, text="✅ Oke, akses kebuka. Sekarang kirim file aja.")


async def save_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    u = update.effective_user
    if not msg or not u:
        return

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

    # serba button: kalau belum join, tampilkan gate (tanpa command lain)
    if (not _is_admin(u.id)) and CFG.force_sub_targets:
        ok = await is_user_joined_all(context, u.id, CFG.force_sub_targets)
        if not ok:
            await _send_gate(msg.chat_id, u.id, context, file_id=None)
            return

    # copy ke DB
    db_chat_id = _pick_db_target()
    try:
        copied = await context.bot.copy_message(
            chat_id=db_chat_id,
            from_chat_id=msg.chat_id,
            message_id=msg.message_id,
        )
    except Exception as e:
        log.exception("copy to db failed: %s", e)
        await msg.reply_text("Gagal simpan ke DB target. Pastikan bot punya akses kirim pesan/admin.")
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
        await msg.reply_text("Bot belum punya username. Set dulu di @BotFather.")
        return

    code = None
    for _ in range(60):
        c = gen_code(10)
        if not STORE.get_file_id_by_code(c):
            code = c
            break

    if not code:
        await msg.reply_text("Gagal generate code unik. Coba ulang.")
        return

    STORE.save_link(code, file_id)

    link = f"https://t.me/{me.username}?start={code}"
    await msg.reply_html(f"<b>Saved.</b>\n<code>{link}</code>", disable_web_page_preview=True)


def main() -> None:
    app: Application = ApplicationBuilder().token(CFG.bot_token).build()

    app.add_handler(CommandHandler("start", start_cmd))
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
