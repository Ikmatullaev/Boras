"""Telegram bot notification provider.

Sends alerts to a Telegram chat via the Bot API. Supports text messages and
optional photo attachments (for snapshots when a person is detected).

Setup:
    1. Create a bot via @BotFather on Telegram, get the API token.
    2. Get your chat_id: send any message to your bot, then visit
       https://api.telegram.org/bot<TOKEN>/getUpdates — look for
       "chat":{"id": <NUMBER>}.
    3. Set environment variables:
        export CRANE_TELEGRAM_TOKEN="123456:ABC-DEF..."
        export CRANE_TELEGRAM_CHAT_ID="123456789"
    4. Notifications auto-enable when both token and chat_id are set.

Message format:
    🚨 Обнаружен человек
    Камера: Boras Security (cam_3)
    Время: 03:15:42
    [photo if available]
    [Access to camera button — if public_base_url configured]

For errors / disconnects, a similar text-only message is sent.
"""
import logging
import time
from datetime import timezone

import requests

from services.notifications.base import NotificationEvent, NotificationProvider

logger = logging.getLogger("crane.notifications.telegram")


# Emoji prefixes by event type — gives quick visual scanning in Telegram
_EVENT_EMOJI = {
    "target_detected": "🚨",
    "target_lost":     "✅",
    "error":           "⚠️",
    "disconnected":    "📡",
    "state_changed":   "🔄",
    "default":         "ℹ️",
}

# Human-readable Russian titles for each event type
_EVENT_TITLE = {
    "target_detected": "Обнаружен человек",
    "target_lost":     "Цель потеряна — возврат в патруль",
    "error":           "Ошибка системы",
    "disconnected":    "Камера отключилась",
    "state_changed":   "Смена режима",
}


class TelegramNotificationProvider(NotificationProvider):
    """Sends notifications to a Telegram chat using the Bot API.

    Uses requests (already a project dependency) so no new deps required.
    Network errors are caught and logged — never raised.

    Multi-camera: includes camera_id and camera_name in every message,
    and (if public_base_url is set) attaches an inline "Access to camera"
    button so the boss can jump straight to the web panel.
    """

    API_BASE = "https://api.telegram.org/bot{token}/{method}"
    REQUEST_TIMEOUT = 10.0  # seconds

    def __init__(self, token: str, chat_id: str, camera_name: str = "360 camera",
                 camera_id: str = "default", public_base_url: str = ""):
        self._token = token
        self._chat_id = chat_id
        self._camera_name = camera_name
        self._camera_id = camera_id
        self._public_base_url = public_base_url.rstrip("/") if public_base_url else ""

    @property
    def name(self) -> str:
        return "telegram"

    def is_configured(self) -> bool:
        return bool(self._token and self._chat_id)

    def send(self, event: NotificationEvent) -> bool:
        if not self.is_configured():
            return False

        text = self._format_text(event)
        inline_keyboard = self._build_inline_keyboard()
        try:
            if event.snapshot:
                return self._send_photo(event.snapshot, text, inline_keyboard)
            return self._send_text(text, inline_keyboard)
        except requests.exceptions.RequestException as e:
            logger.warning("Telegram send failed: %s", e)
            return False
        except Exception as e:
            # Defensive: never let provider crash NotificationService thread
            logger.error("Telegram unexpected error: %s", e)
            return False

    def _format_text(self, event: NotificationEvent) -> str:
        emoji = _EVENT_EMOJI.get(event.event_type, _EVENT_EMOJI["default"])
        title = _EVENT_TITLE.get(event.event_type, "Уведомление")
        # Format time in local timezone (Telegram clients display as-is)
        local_ts = event.timestamp.astimezone()
        time_str = local_ts.strftime("%H:%M:%S")
        date_str = local_ts.strftime("%d.%m.%Y")

        lines = [
            f"{emoji} {title}",
            f"Камера: {self._camera_name} ({self._camera_id})",
            f"Дата: {date_str}",
            f"Время: {time_str}",
        ]
        # Add confidence for target_detected events
        if event.event_type == "target_detected" and event.confidence:
            try:
                conf_pct = float(event.confidence) * 100
                lines.append(f"Уверенность: {conf_pct:.0f}%")
            except (ValueError, TypeError):
                lines.append(f"Уверенность: {event.confidence}")
        if event.detail and "confidence=" not in event.detail:
            lines.append(f"Детали: {event.detail}")
        return "\n".join(lines)

    def _build_inline_keyboard(self):
        """Build inline keyboard with 'Access to camera' button.

        Returns dict suitable for Telegram sendMessage's reply_markup, or None
        if no public_base_url is configured.
        """
        if not self._public_base_url:
            return None
        # Build URL to web panel (HTTP Basic Auth handled by browser)
        url = self._public_base_url
        return {
            "inline_keyboard": [[
                {"text": "🎥 Доступ к камере", "url": url}
            ]]
        }

    def _send_text(self, text: str, reply_markup=None) -> bool:
        url = self.API_BASE.format(token=self._token, method="sendMessage")
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        resp = requests.post(url, json=payload, timeout=self.REQUEST_TIMEOUT)
        ok = resp.status_code == 200
        if not ok:
            logger.warning("Telegram sendMessage -> HTTP %s: %s",
                           resp.status_code, resp.text[:200])
        return ok

    def _send_photo(self, jpeg_bytes: bytes, caption: str,
                    reply_markup=None) -> bool:
        url = self.API_BASE.format(token=self._token, method="sendPhoto")
        data = {"chat_id": self._chat_id, "caption": caption}
        if reply_markup:
            data["reply_markup"] = reply_markup
        resp = requests.post(
            url,
            data=data,
            files={"photo": ("snapshot.jpg", jpeg_bytes, "image/jpeg")},
            timeout=self.REQUEST_TIMEOUT,
        )
        ok = resp.status_code == 200
        if not ok:
            logger.warning("Telegram sendPhoto -> HTTP %s: %s",
                           resp.status_code, resp.text[:200])
            # Fallback: send text-only message so the alert still goes through
            logger.info("Falling back to text-only Telegram message")
            return self._send_text(caption, reply_markup)
        return ok
