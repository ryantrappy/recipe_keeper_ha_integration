"""Read-only meal calendar; explicit actions provide guarded meal edits."""

from datetime import date, datetime, timedelta

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.util import dt as dt_util

from .entity import RecipeKeeperEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities([RecipeKeeperCalendar(entry.runtime_data)])


class RecipeKeeperCalendar(RecipeKeeperEntity, CalendarEntity):
    _attr_translation_key = "meal_plan"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "meal_plan")

    def _events(self) -> list[CalendarEvent]:
        return [
            CalendarEvent(
                start=date.fromisoformat(meal["date"]),
                end=date.fromisoformat(meal["date"]) + timedelta(days=1),
                summary=meal["title"],
                description="\n".join(filter(None, (meal["meal_type"], meal["notes"]))),
                uid=meal["id"],
            )
            for meal in self.coordinator.data["calendar_meals"]
        ]

    @property
    def event(self) -> CalendarEvent | None:
        today = dt_util.now().date()
        return next((event for event in self._events() if event.end > today), None)

    async def async_get_events(
        self, hass, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        start = dt_util.as_local(start_date)
        end = dt_util.as_local(end_date)
        events = self._events()
        return [
            event
            for event in events
            if dt_util.start_of_local_day(event.start) < end
            and dt_util.start_of_local_day(event.end) > start
        ]
