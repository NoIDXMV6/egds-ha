"""Диагностика: предупреждения, ограниченные по частоте.

Пустое окно камеры — частая жалоба, а причины были видны только в
debug-журнале. Теперь каждая неудача снимка/потока попадает в журнал
HA как WARNING, но не чаще одного раза за интервал (чтобы не заспамить
журнал при опросе раз в секунду).
"""
from __future__ import annotations

import logging
import time

_LAST_WARN: dict[str, float] = {}

#: стандартный интервал повторных предупреждений (секунд)
WARN_INTERVAL = 300.0


def warn_limited(
    logger: logging.Logger,
    key: str,
    msg: str,
    *args: object,
    interval: float = WARN_INTERVAL,
) -> None:
    """WARNING-сообщение не чаще одного раза за interval секунд на key."""
    now = time.monotonic()
    if now - _LAST_WARN.get(key, 0.0) >= interval:
        _LAST_WARN[key] = now
        logger.warning(msg, *args)


def reset_limited() -> None:
    """Сбросить ограничения (для тестов)."""
    _LAST_WARN.clear()
