from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


class ModCb(CallbackData, prefix="mod"):
    action: str  # pub | queue | skip | noop
    item_id: int


def review_keyboard(item_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Опублікувати зараз", callback_data=ModCb(action="pub", item_id=item_id))
    kb.button(text="🕒 У чергу", callback_data=ModCb(action="queue", item_id=item_id))
    kb.button(text="❌ Пропустити", callback_data=ModCb(action="skip", item_id=item_id))
    kb.adjust(1, 2)
    return kb.as_markup()


def status_keyboard(item_id: int, label: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=label, callback_data=ModCb(action="noop", item_id=item_id))
    return kb.as_markup()
