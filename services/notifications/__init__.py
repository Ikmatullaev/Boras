"""Notification providers for Boras security alerts.

Architecture:
    Detection → EventLog → NotificationService → NotificationProvider → External service

To add a new provider (Email, WhatsApp, webhook, etc.), create a new class
that inherits from NotificationProvider and implement the send() method.
Register it in NotificationService.__init__.
"""
from services.notifications.base import NotificationProvider, NotificationEvent
from services.notifications.telegram_provider import TelegramNotificationProvider

__all__ = [
    "NotificationProvider",
    "NotificationEvent",
    "TelegramNotificationProvider",
]
