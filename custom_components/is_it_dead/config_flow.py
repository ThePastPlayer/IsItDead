"""Config flow for Is It Dead? integration."""
from __future__ import annotations

import logging
from typing import Any
import voluptuous as vol
import yaml

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector
from homeassistant.helpers.storage import Store

from .const import (
    CONF_CUSTOM_TIMEOUTS,
    CONF_EXCLUDED_ENTITIES,
    CONF_LEARNING_PERIOD,
    CONF_MAX_TIMEOUT,
    CONF_MIN_TIMEOUT,
    CONF_MONITORED_DOMAINS,
    CONF_MULTIPLIER,
    CONF_UPDATE_INTERVAL,
    DEFAULT_LEARNING_PERIOD,
    DEFAULT_MAX_TIMEOUT,
    DEFAULT_MIN_TIMEOUT,
    DEFAULT_MONITORED_DOMAINS,
    DEFAULT_MULTIPLIER,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    STORAGE_KEY,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)


class IsItDeadConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Is It Dead?."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial user setup step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            if not user_input.get(CONF_MONITORED_DOMAINS):
                errors[CONF_MONITORED_DOMAINS] = "invalid_domains"
            else:
                return self.async_create_entry(title="Is It Dead?", data=user_input)

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_MONITORED_DOMAINS, default=DEFAULT_MONITORED_DOMAINS
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=["sensor", "binary_sensor", "device_tracker", "climate"],
                        multiple=True,
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
                vol.Required(
                    CONF_LEARNING_PERIOD, default=DEFAULT_LEARNING_PERIOD
                ): vol.All(vol.Coerce(int), vol.Range(min=1)),
                vol.Required(CONF_MULTIPLIER, default=DEFAULT_MULTIPLIER): vol.All(
                    vol.Coerce(float), vol.Range(min=1.1)
                ),
                vol.Required(CONF_MIN_TIMEOUT, default=DEFAULT_MIN_TIMEOUT): vol.All(
                    vol.Coerce(float), vol.Range(min=0.1)
                ),
                vol.Required(CONF_MAX_TIMEOUT, default=DEFAULT_MAX_TIMEOUT): vol.All(
                    vol.Coerce(float), vol.Range(min=1.0)
                ),
                vol.Required(
                    CONF_UPDATE_INTERVAL, default=DEFAULT_UPDATE_INTERVAL
                ): vol.All(vol.Coerce(int), vol.Range(min=1)),
            }
        )

        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Get the options flow for this handler."""
        return IsItDeadOptionsFlowHandler()


class IsItDeadOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle an options flow for Is It Dead?."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the options flow step."""
        errors: dict[str, str] = {}

        # Get current configuration values
        current_domains = self.config_entry.options.get(
            CONF_MONITORED_DOMAINS,
            self.config_entry.data.get(
                CONF_MONITORED_DOMAINS, DEFAULT_MONITORED_DOMAINS
            ),
        )
        current_learning = self.config_entry.options.get(
            CONF_LEARNING_PERIOD,
            self.config_entry.data.get(
                CONF_LEARNING_PERIOD, DEFAULT_LEARNING_PERIOD
            ),
        )
        current_multiplier = self.config_entry.options.get(
            CONF_MULTIPLIER,
            self.config_entry.data.get(CONF_MULTIPLIER, DEFAULT_MULTIPLIER),
        )
        current_min = self.config_entry.options.get(
            CONF_MIN_TIMEOUT,
            self.config_entry.data.get(CONF_MIN_TIMEOUT, DEFAULT_MIN_TIMEOUT),
        )
        current_max = self.config_entry.options.get(
            CONF_MAX_TIMEOUT,
            self.config_entry.data.get(CONF_MAX_TIMEOUT, DEFAULT_MAX_TIMEOUT),
        )
        current_interval = self.config_entry.options.get(
            CONF_UPDATE_INTERVAL,
            self.config_entry.data.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL),
        )
        current_excluded = self.config_entry.options.get(
            CONF_EXCLUDED_ENTITIES, []
        )
        current_custom = self.config_entry.options.get(CONF_CUSTOM_TIMEOUTS, "")

        if user_input is not None:
            # Validate Custom Timeouts YAML
            custom_timeouts = user_input.get(CONF_CUSTOM_TIMEOUTS, "")
            if custom_timeouts.strip():
                try:
                    parsed = yaml.safe_load(custom_timeouts)
                    if not isinstance(parsed, dict):
                        errors[CONF_CUSTOM_TIMEOUTS] = "invalid_yaml"
                    else:
                        for k, v in parsed.items():
                            if not isinstance(k, str):
                                errors[CONF_CUSTOM_TIMEOUTS] = "invalid_yaml"
                                break
                            try:
                                float(v)
                            except (ValueError, TypeError):
                                errors[CONF_CUSTOM_TIMEOUTS] = "invalid_yaml"
                                break
                except Exception:
                    errors[CONF_CUSTOM_TIMEOUTS] = "invalid_yaml"

            if not user_input.get(CONF_MONITORED_DOMAINS):
                errors[CONF_MONITORED_DOMAINS] = "invalid_domains"

            if not errors:
                return self.async_create_entry(title="", data=user_input)

        # Load storage data to find proposed exclusions (never reported entities)
        store = Store(self.hass, STORAGE_VERSION, STORAGE_KEY)
        learned_data = await store.async_load() or {}
        entities_data = learned_data.get("entities", {})

        # Exclude proposed entities (count == 0) by default in the UI suggestions
        proposed_exclusions = []
        for entity_id, info in entities_data.items():
            if info.get("count", 0) == 0:
                proposed_exclusions.append(entity_id)

        # Merge current excluded list and proposed exclusions
        suggested_exclusions = list(set(current_excluded + proposed_exclusions))

        options_schema = vol.Schema(
            {
                vol.Required(
                    CONF_MONITORED_DOMAINS, default=current_domains
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=["sensor", "binary_sensor", "device_tracker", "climate"],
                        multiple=True,
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
                vol.Required(
                    CONF_LEARNING_PERIOD, default=current_learning
                ): vol.All(vol.Coerce(int), vol.Range(min=1)),
                vol.Required(
                    CONF_MULTIPLIER, default=current_multiplier
                ): vol.All(vol.Coerce(float), vol.Range(min=1.1)),
                vol.Required(CONF_MIN_TIMEOUT, default=current_min): vol.All(
                    vol.Coerce(float), vol.Range(min=0.1)
                ),
                vol.Required(CONF_MAX_TIMEOUT, default=current_max): vol.All(
                    vol.Coerce(float), vol.Range(min=1.0)
                ),
                vol.Required(
                    CONF_UPDATE_INTERVAL, default=current_interval
                ): vol.All(vol.Coerce(int), vol.Range(min=1)),
                vol.Optional(
                    CONF_EXCLUDED_ENTITIES, default=suggested_exclusions
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain=current_domains,
                        multiple=True,
                    )
                ),
                vol.Optional(
                    CONF_CUSTOM_TIMEOUTS, default=current_custom
                ): selector.TextSelector(
                    selector.TextSelectorConfig(multiline=True)
                ),
            }
        )

        return self.async_show_form(
            step_id="init", data_schema=options_schema, errors=errors
        )
