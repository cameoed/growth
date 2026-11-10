"""Platform hold for successful company and contact collection."""

from datetime import timedelta

REHYDRATION_DAYS = 90
REHYDRATION_HOURS = REHYDRATION_DAYS * 24
REHYDRATION_INTERVAL = timedelta(days=REHYDRATION_DAYS)
