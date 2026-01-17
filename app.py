from __future__ import annotations

import logging
import os
import random
from uuid import uuid4
from typing import List, Dict, Any, Tuple

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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

# ===== POST MULTI SELECT ENV =====
def _parse_chat_ids_csv(raw: str) -> List[int]:
    out: List[int] = []
    for part in (raw or "").split(","):
        p = part.strip()
        if not p:
            continue
        if p.startswith("-") and p[1:].isdigit():
            out.append(int(p))
        elif p.isdigit():
            out.append(int(p))
    return out

POST_CHANNEL_IDS = _parse_chat_ids_csv(os.getenv("POST_CHANNEL_IDS", "").strip())
POST_CHANNEL_TITLES = [x.strip() for x in os.getenv("POST_CHANNEL_TITLES", "").split(",") if x.strip()]

# Callback prefixes
CB_POST_TOGGLE = "post_tgl"
CB_POST_SEND = "post_send"
CB_POST_CANCEL = "post_cancel"

# user_data keys
UD_POST_SESS = "post_sessions"  # dict[token] = {"chat_id":int, "msg_id":int, "sel":set[int]}


def _mention_html(user) -> str:
    name = (user.first_name or "bro").replace("<", "").replace(">", "")
    return f"<a href='tg://user?id={user.id}'>{name}</a>"


def _pick_db_target() -> int:
    return random.choice(CFG.db_targets)


def _get_post_titles() -> List[str]:
    titles: List[str] = []
    for i in range(len(POST_CHANNEL_IDS)):
        if i < len(POST_CHANNEL_TITLES):
            titles.append(POST_CHANNEL_TITLES[i])
        else:
            titles.append(f"CH{i+1}")
    return titles


def _get_sessions(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Dict[str, Any]]:
    m = context.user_data.get(UD_POST_SESS)
    if not isinstance(m, dict):
        m = {}
        context.user_data[UD_POST_SESS] = m
    return m


def _build_post_keyboard(token: str, selected: set[int]) -> InlineKeyboardMarkup:
    titles = _get_post_titles()
    rows: List[List[InlineKeyboardButton]] = []

    # tampil 1 tombol per row biar rapih (bisa kamu ubah jadi 2 per row kalau mau)
    for i, title in enumerate(titles):
        mark = "✅" if i in selected else "☑️"
        rows.append([InlineKeyboardButton(f"{mark} {title}", callback_data=f"{CB_POST_TOGGLE}:{token}:{i}")])

    rows.append([
        InlineKeyboardButton("🚀 Kirim", callback_data=f"{CB_POST_SEND}:{token}"),
        InlineKeyboardButton("✖️ Batal", callback_data=f"{CB_POST_CANCEL}:{token}"),
    ])
    return InlineKeyboardMarkup(rows)


async def _post_to_targets(
    context: ContextTypes.DEFAULT_TYPE,
    from_chat_id: int,
    from_message_id: int,
    target_chat_ids: List[int],
) -> Tuple[int, List[int]]:
    ok = 0
    failed: List[int] = []
    for ch in target_chat_ids:
        try:
            await context.bot.copy_message(
                chat_id=ch,
                from_chat_id=from_chat_id,
                message_id=from_message_id,
            )
            ok += 1
        except Exception:
            failed.append(ch)
    return ok, failed


# ===== FSUB gate helpers =====
async def _send_gate(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE, file_id: str | None) -> None:
    """
    Gate FSUB hanya untuk akses file (redeem).
    AUTO-ROTATE per file + tracking skip tetap aktif.
    """
    gate_key = file_id or "__none__"
    st = STORE.get_user_state(user_id)

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


# ===== Commands / Callbacks =====
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /start tanpa payload: welcome saja (tidak gate).
    /start <CODE>: redeem file -> kalau belum join, baru gate.
    """
    if not update.message or not update.effective_user:
        return

    u = update.effective_user
    args = context.args

    if not args:
        text = CFG.start_message.format(mention=_mention_html(u))
        await update.message.reply_html(text, disable_web_page_preview=True)
        return

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
    try:
        await q.message.delete()
    except Exception:
        pass

    STORE.set_last_gate_key(q.from_user.id, "")

    if file_id:
        await _send_file(q.message.chat_id, context, file_id)
    else:
        await context.bot.send_message(chat_id=q.message.chat_id, text="✅ Oke, akses kebuka.")


# ===== POST SELECT CALLBACKS =====
async def cb_post_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return

    data = (q.data or "")
    # post_tgl:token:index
    try:
        _, token, idx_str = data.split(":", 2)
        idx = int(idx_str)
    except Exception:
        await q.answer()
        return

    sess = _get_sessions(context).get(token)
    if not sess:
        await q.answer("Session expired.", show_alert=True)
        return

    selected: set[int] = sess["sel"]
    if idx in selected:
        selected.remove(idx)
    else:
        selected.add(idx)

    await q.answer()
    try:
        await q.edit_message_reply_markup(reply_markup=_build_post_keyboard(token, selected))
    except Exception:
        pass


async def cb_post_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return

    data = (q.data or "")
    # post_send:token
    try:
        _, token = data.split(":", 1)
    except Exception:
        await q.answer()
        return

    sessions = _get_sessions(context)
    sess = sessions.get(token)
    if not sess:
        await q.answer("Session expired.", show_alert=True)
        return

    selected: set[int] = sess["sel"]
    if not selected:
        await q.answer("Pilih minimal 1 channel.", show_alert=True)
        return

    targets = [POST_CHANNEL_IDS[i] for i in sorted(selected) if 0 <= i < len(POST_CHANNEL_IDS)]
    ok_count, failed = await _post_to_targets(context, sess["chat_id"], sess["msg_id"], targets)

    await q.answer()

    # edit message jadi hasil
    if failed:
        await q.edit_message_text(f"✅ Terkirim: {ok_count}\n❌ Gagal: {len(failed)} (cek izin bot di target)")
    else:
        await q.edit_message_text(f"✅ Terkirim ke {ok_count} channel.")

    sessions.pop(token, None)


async def cb_post_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return

    data = (q.data or "")
    # post_cancel:token
    try:
        _, token = data.split(":", 1)
    except Exception:
        await q.answer()
        return

    sessions = _get_sessions(context)
    sessions.pop(token, None)

    await q.answer()
    try:
        await q.edit_message_text("Dibatalkan.")
    except Exception:
        pass


# ===== UPLOAD HANDLER =====
async def save_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Upload otomatis:
    - Simpan ke DB (selalu)
    - Balas link /start=CODE
    - Kalau POST_CHANNEL_IDS ada -> kirim tombol select untuk post ke channel tertentu
    """
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

    # 1) Save to DB target
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

    # 2) Create link
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

    # Balas link (ini tetap)
    await msg.reply_html(f"<b>Saved.</b>\n<code>{link}</code>", disable_web_page_preview=True)

    # 3) Post select buttons (kalau diset)
    if POST_CHANNEL_IDS:
        token = gen_code(12)

        sessions = _get_sessions(context)
        sessions[token] = {
            "chat_id": msg.chat_id,
            "msg_id": msg.message_id,   # ini message media asli user, yang akan di-copy
            "sel": set(),              # awalnya kosong (user pilih sendiri)
        }

        # kirim menu button
        await msg.reply_text(
            "Pilih channel tujuan upload:",
            reply_markup=_build_post_keyboard(token, sessions[token]["sel"]),
        )


def main() -> None:
    app: Application = ApplicationBuilder().token(CFG.bot_token).build()

    # core
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(done_cb, pattern=r"^fsub_done:"))

    # post select
    app.add_handler(CallbackQueryHandler(cb_post_toggle, pattern=r"^post_tgl:"))
    app.add_handler(CallbackQueryHandler(cb_post_send, pattern=r"^post_send:"))
    app.add_handler(CallbackQueryHandler(cb_post_cancel, pattern=r"^post_cancel:"))

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
