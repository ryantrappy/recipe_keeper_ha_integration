"""Account identity, authentication errors, and opt-in editing options."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.recipe_keeper._vendor.recipe_keeper_client import cloud_protocol

AuthenticationError = cloud_protocol.AuthenticationError
ProbeError = cloud_protocol.ProbeError


async def test_user_default_readonly_and_duplicate(hass: HomeAssistant) -> None:
    with (
        patch(
            "custom_components.recipe_keeper.config_flow.AsyncRecipeClient",
            autospec=True,
        ) as factory,
        patch("custom_components.recipe_keeper.async_setup_entry", return_value=True),
    ):
        factory.return_value.authenticate.return_value = "account-id"
        result = await hass.config_entries.flow.async_init(
            "recipe_keeper",
            context={"source": config_entries.SOURCE_USER},
            data={"email": " test@example.invalid ", "password": "synthetic"},
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["data"]["email"] == "test@example.invalid"
        assert result["options"] == {"allow_edits": False, "pantry_staples": []}
        await hass.async_block_till_done()
        duplicate = await hass.config_entries.flow.async_init(
            "recipe_keeper",
            context={"source": config_entries.SOURCE_USER},
            data={"email": "test@example.invalid", "password": "synthetic"},
        )
        assert duplicate["reason"] == "already_configured"
        assert factory.return_value.close.await_count == 2


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (AuthenticationError("rejected"), "invalid_auth"),
        (ProbeError("offline"), "cannot_connect"),
    ],
)
async def test_authentication_errors(
    hass: HomeAssistant, error: Exception, expected: str
) -> None:
    with patch(
        "custom_components.recipe_keeper.config_flow.AsyncRecipeClient", autospec=True
    ) as factory:
        factory.return_value.authenticate.side_effect = error
        result = await hass.config_entries.flow.async_init(
            "recipe_keeper",
            context={"source": config_entries.SOURCE_USER},
            data={"email": "test@example.invalid", "password": "synthetic"},
        )
        assert result["errors"] == {"base": expected}
        factory.return_value.close.assert_awaited_once()


async def test_options_and_reauthentication(
    hass: HomeAssistant, entry: MockConfigEntry, client: AsyncMock
) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    with patch(
        "homeassistant.config_entries.ConfigEntries.async_reload", return_value=True
    ) as reload:
        result = await hass.config_entries.options.async_init(
            entry.entry_id,
            data={
                "allow_edits": True,
                "pantry_staples": "olive oil\nSalt\n",
                "todo_entity": "todo.test",
            },
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
        assert entry.options == {
            "allow_edits": True,
            "pantry_staples": ["olive oil", "Salt"],
            "todo_entity": "todo.test",
        }
        reload.assert_awaited_once()
    with (
        patch(
            "custom_components.recipe_keeper.config_flow.AsyncRecipeClient",
            autospec=True,
        ) as factory,
        patch(
            "homeassistant.config_entries.ConfigEntries.async_reload", return_value=True
        ),
    ):
        factory.return_value.authenticate.return_value = entry.unique_id
        initial = await hass.config_entries.flow.async_init(
            "recipe_keeper",
            context={
                "source": config_entries.SOURCE_REAUTH,
                "entry_id": entry.entry_id,
            },
            data=entry.data,
        )
        result = await hass.config_entries.flow.async_configure(
            initial["flow_id"], {"password": "new-synthetic"}
        )
        assert result["reason"] == "reauth_successful"
        assert entry.data["password"] == "new-synthetic"
