"""Constants for the Is It Dead? integration."""

DOMAIN = "is_it_dead"
PLATFORMS = ["binary_sensor"]

# Configuration keys
CONF_MONITORED_DOMAINS = "monitored_domains"
CONF_LEARNING_PERIOD = "learning_period"  # in days
CONF_MULTIPLIER = "multiplier"  # threshold multiplier
CONF_EXCLUDED_ENTITIES = "excluded_entities"
CONF_CUSTOM_TIMEOUTS = "custom_timeouts"  # YAML overrides
CONF_MIN_TIMEOUT = "min_timeout"  # in hours
CONF_MAX_TIMEOUT = "max_timeout"  # in hours
CONF_UPDATE_INTERVAL = "update_interval"  # check interval in minutes

# Defaults
DEFAULT_MONITORED_DOMAINS = ["sensor", "binary_sensor"]
DEFAULT_LEARNING_PERIOD = 7  # days
DEFAULT_MULTIPLIER = 3.0
DEFAULT_MIN_TIMEOUT = 1.0  # hour
DEFAULT_MAX_TIMEOUT = 168.0  # 7 days (1 week)
DEFAULT_UPDATE_INTERVAL = 15  # minutes

# Storage settings
STORAGE_KEY = f"{DOMAIN}.learned_data"
STORAGE_VERSION = 1
