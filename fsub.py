from __future__ import annotations

import random
from typing import Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes


def split_target(raw: str) -> Tuple[str, Optional[str]]:
    """
    Returns: (check_chat, join_url_or_none)
    Accept:
      - "@public" -> ("@public", "https://t.me/public")
      - "-100id|https://t.me/+invite" -> ("-100id", "https://t.me/+invite")
      - "-100id" -> ("-100id", None) -> will require auto invite link
    """
    s = str(raw).strip()
    if "|" in s:
        a, b = s.split("|", 1)
        return a.strip(), b.strip()
    if s.startswith("@"):
        return s, f"https://t.me/{s.lstrip('@')}"
    return s, None


async def is_user_joined_all(context: ContextTypes.DEFAULT_TYPE, user_id: int, targets: list[str]) -> bool:
    if not targets:
        return True
    for raw in targets:
        check_chat, _ = split_target(raw)
        try:
            member = await context.bot.get_chat_member(chat_id=check_chat, user_id=user_id)
            status = getattr(member, "status", None)
            if status in ("left", "kicked"):
                return False
        except Exception:
            return False
    return True


def visible_targets_for_user(targets: list[str], user_id: int, offset: int, k: int) -> list[str]:
    """
    Per-user random, stable by (user_id, offset).
    offset naik = "batch berikutnya".
    """
    if not targets:
        return []
    k = max(1, min(k, len(targets)))

    seed = (user_id * 1000003) ^ (offset * 9176) ^ len(targets)
    rng = random.Random(seed)

    arr = targets[:]
    rng.shuffle(arr)
    return arr[:k]


async def build_join_keyboard(
    context: ContextTypes.DEFAULT_TYPE,
    targets: list[str],
    user_id: int,
    offset: int,
    buttons_per_row: int,
    join_text: str,
    done_callback_data: str,
    max_buttons: int = 4,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    buf: list[InlineKeyboardButton] = []

    visible = visible_targets_for_user(targets, user_id=user_id, offset=offset, k=max_buttons)

    idx = 1
    for raw in visible:
        check_chat, join_url = split_target(raw)

        if not join_url:
            try:
                invite = await context.bot.create_chat_invite_link(chat_id=check_chat, creates_join_request=False)
                join_url = invite.invite_link
            except Exception:
                continue

        buf.append(InlineKeyboardButton(f"{join_text} {idx}", url=join_url))
        idx += 1

        if len(buf) >= buttons_per_row:
            rows.append(buf)
            buf = []

    if buf:
        rows.append(buf)

    rows.append([InlineKeyboardButton("✅ Sudah Join", callback_data=done_callback_data)])
    return InlineKeyboardMarkup(rows)
