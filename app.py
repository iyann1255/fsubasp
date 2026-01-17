from __future__ import annotations

import logging
import random
import time
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
from fsub import build_join_keyboard, is_user_joined_all, _split_target  # type: ignore
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
CB_ROTATE = "fsub_rot"
CB_NOOP = "noop"

def _mention_html(user) -> str:
    name = (user.first_name or "bro").replace("<", "").replace(">", "")
    return f"<a href='tg://user?id={user.id}'>{name}</a>"

def _is_admin(user_id: int) -> bool:
    return user_id in CFG.admins

def _pick_db_target() -> int:
    return random.choice(CFG.db_targets)

async def _send_gate(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE, file_id: str | None) -> None:
    # per-user offset for rotation
    st = STORE.get_user_state(user_id)
    offset = st.offset

    done_data = f"{CB_DONE}:{file_id}" if file_id else f"{CB_DONE}:__none__"
    rot_data = f"{CB_ROTATE}:{file_id or '__none__'}"

    kb = await build_join_keyboard(
        context=context,
        targets=CFG.force_sub_targets,
        user_id=user_id,
        offset=offset,
        buttons_per_row=CFG.buttons_per_row,
        join_text=CFG.join_text,
        done_callback_data=done_data,
        rotate_callback_data=rot_data,
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
    # /start wajib untuk deep-link
    if not update.message or not update.effective_user:
        return

    u = update.effective_user
    args = context.args

    if not args:
        # no command vibe: hanya info + tombol (kalau fsub ada, langsung gate)
        if CFG.force_sub_targets:
            await _send_gate(update.message.chat_id, u.id, context, file_id=None)
            return
        text = CFG.start_message.format(mention=_mention_html(u))
        await update.message.reply_html(text, disable_web_page_preview=True)
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

async def cb_rotate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not q.from_user or not q.message:
        return
    data = (q.data or "")
    if not data.startswith(f"{CB_ROTATE}:"):
        await q.answer()
        return

    user_id = q.from_user.id
    st = STORE.get_user_state(user_id)
    now = int(time.time())

    # delay rotate
    wait = CFG.rotate_seconds - (now - st.last_rotated_ts)
    if wait > 0:
        await q.answer(f"Tunggu {wait} detik lagi.", show_alert=True)
        return

    # tracking: hitung skip untuk tombol yang sedang tampil (visible set = offset saat ini)
    # Kita anggap rotate = "melewatkan" subset yang sedang ditawarkan.
    visible_raw = []
    # re-build current visible set using current offset
    # (kita pakai logic yang sama dari fsub._pick_visible_for_user via build_join_keyboard,
    # tapi untuk tracking cukup ambil subset dari targets berdasarkan seed user+offset)
    # Cara paling aman: ambil dari CFG.force_sub_targets, shuffle pake seed yg sama
    seed = (user_id * 1000003) ^ (st.offset * 9176) ^ len(CFG.force_sub_targets)
    rng = random.Random(seed)
    arr = CFG.force_sub_targets[:]
    rng.shuffle(arr)
    visible_raw = arr[: max(1, min(CFG.max_join_buttons, len(arr)))]

    for raw in visible_raw:
        check_chat, _join_url = _split_target(raw)  # type: ignore
        STORE.inc_skip(str(check_chat), 1)

    # bump offset + update ts
    new_st = STORE.bump_rotate(user_id)

    # edit gate message with new keyboard
    file_id = data.split(":", 1)[1].strip()
    if file_id == "__none__":
        file_id = None

    done_data = f"{CB_DONE}:{file_id}" if file_id else f"{CB_DONE}:__none__"
    rot_data = f"{CB_ROTATE}:{file_id or '__none__'}"

    kb = await build_join_keyboard(
        context=context,
        targets=CFG.force_sub_targets,
        user_id=user_id,
        offset=new_st.offset,
        buttons_per_row=CFG.buttons_per_row,
        join_text=CFG.join_text,
        done_callback_data=done_data,
        rotate_callback_data=rot_data,
        max_buttons=CFG.max_join_buttons,
    )

    await q.answer()
    try:
        await q.message.edit_reply_markup(reply_markup=kb)
    except Exception:
        # kalau edit gagal (message old), kirim gate baru
        await _send_gate(q.message.chat_id, user_id, context, file_id=file_id)

async def cb_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
    # bersihin gate message biar rapi
    try:
        await q.message.delete()
    except Exception:
        pass

    if file_id:
        await _send_file(q.message.chat_id, context, file_id)
    else:
        # no command vibe: kalau cuma gate start, ya kasih notif
        await context.bot.send_message(chat_id=q.message.chat_id, text="✅ Oke, akses kebuka. Sekarang kirim file aja.")

async def save_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    u = update.effective_user
    if not msg or not u:
        return

    # detect kind
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

    # serba tombol: kalau belum join, jangan ceramah, langsung tampil gate buttons
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

    # code unik
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
    # no command vibe: balas singkat + tombol copy via text
    await msg.reply_html(
        f"<b>Saved.</b>\n<code>{link}</code>",
        disable_web_page_preview=True,
    )

def main() -> None:
    app: Application = ApplicationBuilder().token(CFG.bot_token).build()

    app.add_handler(CommandHandler("start", start_cmd))

    # callbacks
    app.add_handler(CallbackQueryHandler(cb_rotate, pattern=r"^fsub_rot:"))
    app.add_handler(CallbackQueryHandler(cb_done, pattern=r"^fsub_done:"))

    # uploads
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
