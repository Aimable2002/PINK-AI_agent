"""
Registry mapping a service_id string (as stored in
user_agent_services.service_id) to its implementation module. This is
the ONLY shared indirection -- the daemon and Celery tasks look a module
up here by name rather than hardcoding per-service imports everywhere.
Adding a new agent service means writing its file and adding one line
here; nothing else in the daemon or task layer needs to change.
"""

from app.agent_services import telegram_signal_monitor

REGISTRY = {
    telegram_signal_monitor.SERVICE_ID: telegram_signal_monitor,
}