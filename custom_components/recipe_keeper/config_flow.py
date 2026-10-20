"""Account setup, reauthentication, and administrator options."""

import json
from collections.abc import Mapping
from pathlib import Path

import probatio as vol
from homeassistant.config_entries import ConfigFlow, OptionsFlow
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from ._vendor.recipe_keeper_client.async_client import AsyncRecipeClient
from ._vendor.recipe_keeper_client.cloud_backend import Credentials
from ._vendor.recipe_keeper_client.cloud_protocol import AuthenticationError, ProbeError
from ._vendor.recipe_keeper_client.shopping import parse_staples
from .const import CONF_ALLOW_EDITS, CONF_PANTRY_STAPLES, CONF_TODO_ENTITY, DOMAIN

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(vol.Secret(CONF_PASSWORD)): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)


class RecipeKeeperConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def _validate(self, data: Mapping) -> tuple[dict, str | None]:
        credentials = Credentials(data[CONF_EMAIL], data[CONF_PASSWORD])
        client = AsyncRecipeClient(
            async_get_clientsession(self.hass),
            credentials,
            Path(
                self.hass.config.path(".storage", "recipe_keeper_validation_unused.db")
            ),
            executor=self.hass.async_add_executor_job,
        )
        try:
            return {}, await client.authenticate()
        except AuthenticationError:
            return {"base": "invalid_auth"}, None
        except (ProbeError, OSError, ValueError):
            return {"base": "cannot_connect"}, None
        finally:
            await client.close()

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            data = {
                CONF_EMAIL: user_input[CONF_EMAIL].strip(),
                CONF_PASSWORD: user_input[CONF_PASSWORD],
            }
            errors, account_id = await self._validate(data)
            if not errors:
                await self.async_set_unique_id(account_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="Recipe Keeper",
                    data=data,
                    options={CONF_ALLOW_EDITS: False, CONF_PANTRY_STAPLES: []},
                )
        return self.async_show_form(
            step_id="user", data_schema=USER_SCHEMA, errors=errors
        )

    async def async_step_reauth(self, entry_data: Mapping):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        errors = {}
        if user_input is not None:
            entry = self._get_reauth_entry()
            data = {
                CONF_EMAIL: entry.data[CONF_EMAIL],
                CONF_PASSWORD: user_input[CONF_PASSWORD],
            }
            errors, account_id = await self._validate(data)
            if not errors:
                await self.async_set_unique_id(account_id)
                self._abort_if_unique_id_mismatch()
                return self.async_update_reload_and_abort(entry, data_updates=data)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(vol.Secret(CONF_PASSWORD)): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    )
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return RecipeKeeperOptionsFlow()


class RecipeKeeperOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                staples = parse_staples(
                    json.dumps(
                        [
                            line.strip()
                            for line in user_input[CONF_PANTRY_STAPLES].splitlines()
                            if line.strip()
                        ]
                    )
                )
            except ValueError:
                errors[CONF_PANTRY_STAPLES] = "invalid_staples"
            else:
                options = {
                    CONF_ALLOW_EDITS: user_input[CONF_ALLOW_EDITS],
                    CONF_PANTRY_STAPLES: list(staples),
                }
                if user_input.get(CONF_TODO_ENTITY):
                    options[CONF_TODO_ENTITY] = user_input[CONF_TODO_ENTITY]
                return self.async_create_entry(title="", data=options)
        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_ALLOW_EDITS, default=options.get(CONF_ALLOW_EDITS, False)
                ): bool,
                vol.Required(
                    CONF_PANTRY_STAPLES,
                    default="\n".join(options.get(CONF_PANTRY_STAPLES, [])),
                ): TextSelector(TextSelectorConfig(multiline=True)),
                vol.Optional(
                    CONF_TODO_ENTITY,
                    description={"suggested_value": options.get(CONF_TODO_ENTITY)},
                ): EntitySelector(EntitySelectorConfig(domain="todo")),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
