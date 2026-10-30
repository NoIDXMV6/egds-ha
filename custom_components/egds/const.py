"""Константы интеграции ЕГДС."""
from __future__ import annotations

DOMAIN = "egds"
PLATFORMS = ["button", "cover", "camera"]

MANUFACTURER = "ЕГДС"
MODEL_BARRIER = "КПУ (шлагбаум)"
MODEL_CAMERA = "Камера КПУ"

# Параметры протокола (из приложения ЕГДС 3.3.25, ru.egds.EgdsMobile)
CLIENT_ID = "AndroidApp_1.3"
CLIENT_SECRET = "Secret#1"

URL_API_BASE = "https://client.egdsk.ru"
URL_OAUTH = "/oauth/token"
GRANT_SMS = "grant_type=sms&phone_number={phone}"
GRANT_PASSWORD = "grant_type=password&username={phone}&password={code}"
GRANT_REFRESH = "grant_type=refresh_token&refresh_token={token}"

DEFAULT_TIMEOUT = 30

# Конфигурация
CONF_API_BASE = "api_base"

# Опции
SCAN_INTERVAL = "scan_interval"
OPENING_TIME = "opening_time"
OPEN_DURATION = "open_duration"
COOLDOWN = "cooldown"
VIDEO_FRAMERATE = "video_framerate"
VIDEO_WIDTH = "video_width"

DEFAULT_POLL_INTERVAL = 300
DEFAULT_OPENING_TIME = 10       # секунд "открывается" (исходное положение)
DEFAULT_OPEN_DURATION = 20      # сколько секунд считается открытым
DEFAULT_COOLDOWN = 20           # защита от повторной команды
DEFAULT_VIDEO_FRAMERATE = 2     # кадр/с для MJPEG
DEFAULT_VIDEO_WIDTH = ""        # пусто — размер не передаётся (как у сервера)
