"""Aggregate diagnostics deliberately exclude account and recipe contents."""


async def async_get_config_entry_diagnostics(hass, entry) -> dict:
    coordinator = entry.runtime_data
    return {
        "integration_version": "0.2.1",
        "last_update_success": coordinator.last_update_success,
        "allow_edits": coordinator.client.allow_edits,
        "status": {
            key: value
            for key, value in (coordinator.data or {}).items()
            if key != "calendar_meals"
        },
    }
