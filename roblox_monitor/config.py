import os

# Groups to monitor
MONITORED_GROUPS = {
    485588074: "TSB Air",
    592750791: "TSB Earth",
    1029776236: "TSB Water",
    44315578: "TSB Fire"
}

# Scanning and Syncing Intervals
PRESENCE_SCAN_INTERVAL = 20  # seconds
GROUP_SYNC_INTERVAL = 900  # 15 minutes

# The only game the bot cares about
TARGET_UNIVERSE_ID = 9662757817

# Spike Alert Config
SPIKE_WINDOW_SECONDS = 180  # 3 minutes

# Alert Thresholds (Absolute number of new players in window)
ALERT_THRESHOLD_INFO = 10
ALERT_THRESHOLD_WARNING = 20
ALERT_THRESHOLD_HIGH = 30
ALERT_THRESHOLD_CRITICAL = 50

# Alert Thresholds (Relative % increase in window)
ALERT_PERCENT_INFO = 2.0     # 200%
ALERT_PERCENT_WARNING = 3.0  # 300%
ALERT_PERCENT_HIGH = 4.0     # 400%
ALERT_PERCENT_CRITICAL = 5.0 # 500%

# Cooldown
ALERT_COOLDOWN_MINUTES = 10

# Database Retention (Keep disk usage low due to 512MB limit)
PRESENCE_HISTORY_RETENTION_DAYS = 3
ALERT_HISTORY_RETENTION_DAYS = 7

# The Discord channel ID where spike alerts are sent
ALERTS_CHANNEL_ID = os.getenv('ALERTS_CHANNEL_ID')
# Optional: User ID to DM when a spike occurs
ALERT_USER_ID = os.getenv('ALERT_USER_ID') # Can be set in .env
