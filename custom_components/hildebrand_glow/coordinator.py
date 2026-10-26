"""Data update coordinator for Hildebrand Glow integration."""
from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from homeassistant.components.recorder.models import StatisticData, StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import async_import_statistics, statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from .api import UK_TZ, DailyReading, GlowmarktApiClient, GlowmarktApiError, GlowmarktAuthError
from .const import DOMAIN, DEFAULT_SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)

# Classifiers whose readings represent a single day's usage, not a running
# total. Their sensors use state_class total_increasing, which HA's Energy
# dashboard statistics treat as a cumulative meter reading -- so they need a
# persisted running counter (see _accumulate), not the raw daily value.
CUMULATIVE_CLASSIFIERS = ("electricity.consumption", "gas.consumption")
CUMULATIVE_STORAGE_VERSION = 1
# Every poll makes sure each of the last RECENT_DAYS complete (UK-local) days is
# in the statistics, so a day whose fetch failed or arrived late is picked up on
# a later poll. The last RECENT_RECHECK_DAYS are also re-read about once an hour,
# so a day that was only partly there when first imported gets corrected.
RECENT_DAYS = 14
RECENT_RECHECK_DAYS = 3

class GlowmarktDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Class to manage fetching Glowmarkt data."""

    def __init__(self, hass: HomeAssistant, api_client: GlowmarktApiClient, tariff_config: dict[str, float], entry_id: str) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=DEFAULT_SCAN_INTERVAL)
        self.api_client = api_client
        self.tariff_config = tariff_config
        self._entry_id = entry_id
        self._resources: dict[str, dict[str, Any]] = {}
        self._last_readings: dict[str, DailyReading] = {}  # Cache last known good readings
        self._store: Store = Store(hass, CUMULATIVE_STORAGE_VERSION, f"{DOMAIN}_{entry_id}_cumulative")
        self._cumulative: dict[str, Any] | None = None
        self._cumulative_lock = asyncio.Lock()
        self._backfill_started = False
        self._last_recent_check: datetime | None = None

    def _entity_id_for(self, classifier: str) -> str | None:
        registry = er.async_get(self.hass)
        unique_id = f"{self._entry_id}_{classifier}"
        return registry.async_get_entity_id("sensor", DOMAIN, unique_id)

    @staticmethod
    def _metadata_for(entity_id: str) -> StatisticMetaData:
        return StatisticMetaData(
            has_sum=True,
            mean_type=StatisticMeanType.NONE,
            name=None,
            source="recorder",
            statistic_id=entity_id,
            unit_class=None,
            unit_of_measurement="kWh",
        )

    def _import_hourly_statistics(self, entity_id: str, reading: DailyReading, baseline: float) -> float:
        """Replace this entity's long-term stats for `reading`'s day with its
        real hour-by-hour breakdown, instead of leaving whatever the normal
        state-based compiler recorded there (a single lump on whichever hour
        the coordinator happened to poll and notice the new day).

        Returns the running cumulative total after this day, which becomes
        the baseline for the next day.
        """
        hourly: dict[datetime, float] = {}
        for ts, value in reading.intervals:
            hour_start = ts.replace(minute=0, second=0, microsecond=0)
            hourly[hour_start] = hourly.get(hour_start, 0.0) + value

        running = baseline
        stats: list[StatisticData] = []
        for hour_start in sorted(hourly):
            running += hourly[hour_start]
            stats.append(StatisticData(start=hour_start, state=round(hourly[hour_start], 3), sum=round(running, 3)))

        async_import_statistics(self.hass, self._metadata_for(entity_id), stats)
        return round(running, 3)

    async def _accumulate(self, classifier: str, reading: DailyReading) -> float:
        """Add one day's reading to classifier's persisted running total,
        importing its real hourly breakdown at the same time -- once per
        distinct day, since the coordinator polls every 5 minutes and would
        otherwise redo this on every poll until the API's window rolls over
        to the next day.

        Guarded by _cumulative_lock since backfill (see below) mutates the
        same persisted dict concurrently, in a background task.
        """
        async with self._cumulative_lock:
            if self._cumulative is None:
                self._cumulative = await self._store.async_load() or {}

            entry = self._cumulative.get(classifier, {"day": None, "cumulative": 0.0})
            if entry["day"] == reading.day:
                return entry["cumulative"]

            entity_id = self._entity_id_for(classifier)
            if entity_id is None:
                # Entity not registered yet (e.g. the very first refresh,
                # before platforms are forwarded) -- fall back to a plain
                # add for now and let the hourly breakdown be corrected by
                # the backfill pass once the entity exists.
                _LOGGER.debug("No entity registered yet for %s, deferring hourly import", classifier)
                new_cumulative = round(entry["cumulative"] + reading.value, 3)
            else:
                new_cumulative = self._import_hourly_statistics(entity_id, reading, entry["cumulative"])

            self._cumulative[classifier] = {"day": reading.day, "cumulative": new_cumulative}
            await self._store.async_save(self._cumulative)
            return new_cumulative

    async def _recorded_sum_before(self, entity_id: str, when: datetime) -> float | None:
        """The statistics' running `sum` at the last hour before `when`, or None
        if there is none -- the baseline a re-imported day has to continue from.
        """
        rows = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period, self.hass, when - timedelta(days=7), when, {entity_id}, "hour", None, {"sum"}
        )
        for row in reversed(rows.get(entity_id) or []):
            if row.get("sum") is not None:
                return float(row["sum"])
        return None

    async def _sync_recent_days(self, classifier: str, recheck: bool) -> None:
        """Import each of the last RECENT_DAYS days that is missing (or, about
        once an hour, each of the last RECENT_RECHECK_DAYS), oldest first.

        A day whose fetch fails is not marked as done, so a later poll tries it
        again instead of the day being skipped for good. Once one day is
        (re)imported, every later day in the window is re-imported after it, so
        the statistics' running sum stays continuous; the first one starts from
        the sum the recorder already holds at that day's start, so nothing is
        counted twice.
        """
        resource = self._resources.get(classifier)
        entity_id = self._entity_id_for(classifier)
        if resource is None or entity_id is None:
            return
        async with self._cumulative_lock:
            if self._cumulative is None:
                self._cumulative = await self._store.async_load() or {}
            days: dict[str, float] = self._cumulative.setdefault(f"{classifier}_days", {})

            today_uk = datetime.now(UK_TZ).date()
            window = [today_uk - timedelta(days=back) for back in range(RECENT_DAYS, 0, -1)]

            first = next((i for i, day in enumerate(window)
                          if day.isoformat() not in days or (recheck and (today_uk - day).days <= RECENT_RECHECK_DAYS)), None)
            if first is None:
                return

            first_start = datetime(window[first].year, window[first].month, window[first].day, tzinfo=UK_TZ).astimezone(timezone.utc)
            running = await self._recorded_sum_before(entity_id, first_start)
            if running is None:
                running = self._cumulative.get(classifier, {}).get("cumulative", 0.0)

            imported = []
            for day in window[first:]:
                reading = await self.api_client.get_day_reading(resource["resource_id"], day)
                if reading is None:
                    _LOGGER.debug("%s: no data for %s yet (or the fetch failed); will try again", classifier, day)
                    continue
                running = self._import_hourly_statistics(entity_id, reading, running)
                days[day.isoformat()] = running
                imported.append(day.isoformat())

            if imported:
                # forget days that have dropped out of the window
                for old in [d for d in days if d < window[0].isoformat()]:
                    del days[old]
                self._cumulative[classifier] = {"day": imported[-1], "cumulative": running}
                await self._store.async_save(self._cumulative)
                _LOGGER.debug("%s: imported %s", classifier, ", ".join(imported))

    async def _async_backfill_history(self) -> None:
        """One-time import of every available day of real hourly data, so
        existing Energy dashboard history looks right immediately instead of
        only newly-arriving days getting the accurate breakdown. How far
        back this reaches depends entirely on how much history Glowmarkt
        actually has for the account (see api.get_available_daily_readings).

        Runs as a background task (see _async_update_data) rather than
        blocking the coordinator's first refresh: how much history exists
        varies wildly per account, and blocking HA's startup on possibly
        many months of day-by-day API calls would be a bad experience for
        anyone whose account has more history than this was tested against.
        """
        async with self._cumulative_lock:
            if self._cumulative is None:
                self._cumulative = await self._store.async_load() or {}
            if self._cumulative.get("_backfilled"):
                return

        # Only the two consumption classifiers feed total_increasing
        # sensors and need this -- restricting to them (rather than every
        # discovered resource) avoids also walking the full history of
        # unrelated cost resources, which can be far longer and needlessly
        # slow (one account seen with real cost data going back over 9
        # months, vs. under 2 weeks of consumption data).
        by_classifier = await self.api_client.get_available_readings(set(CUMULATIVE_CLASSIFIERS))

        async with self._cumulative_lock:
            for classifier in CUMULATIVE_CLASSIFIERS:
                entity_id = self._entity_id_for(classifier)
                if entity_id is None:
                    _LOGGER.debug("No entity registered yet for %s, deferring backfill", classifier)
                    return  # retry the whole backfill next poll, once entities exist

                baseline = 0.0
                entry = self._cumulative.get(classifier, {"day": None, "cumulative": 0.0})
                for reading in by_classifier.get(classifier, []):
                    baseline = self._import_hourly_statistics(entity_id, reading, baseline)
                    entry = {"day": reading.day, "cumulative": baseline}
                self._cumulative[classifier] = entry

                # The normal state-based compiler may already have written
                # stale values for today's hours so far (using whatever flat
                # total the entity showed before this backfill raised its
                # running total). Overwrite the whole of today-so-far with
                # flat, zero-usage points at the corrected baseline, so the
                # dashboard doesn't show a bogus dip/spike between the
                # backfilled history and the currently-forming (not yet
                # complete) day.
                today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
                current_hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
                hours_elapsed_today = int((current_hour - today_start).total_seconds() // 3600) + 1
                gap_stats = [
                    StatisticData(start=today_start + timedelta(hours=h), state=0.0, sum=entry["cumulative"])
                    for h in range(hours_elapsed_today)
                ]
                async_import_statistics(self.hass, self._metadata_for(entity_id), gap_stats)

            self._cumulative["_backfilled"] = True
            await self._store.async_save(self._cumulative)
            _LOGGER.info(
                "Backfilled hourly statistics history: %s",
                ", ".join(f"{c}={len(r)} day(s)" for c, r in by_classifier.items())
            )

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            if not self._resources:
                self._resources = await self.api_client.discover_resources()

            if not self._backfill_started:
                self._backfill_started = True
                self.hass.async_create_task(self._async_backfill_history(), name=f"{DOMAIN} history backfill")

            readings = await self.api_client.get_all_readings()

            # Merge with cached readings - only update if we got valid data
            for key, reading in readings.items():
                if reading is not None:
                    self._last_readings[key] = reading
                    _LOGGER.debug("Updated %s to %.3f (day %s)", key, reading.value, reading.day)
                elif key in self._last_readings:
                    _LOGGER.debug("Keeping cached value for %s: %.3f (API returned None)",
                        key, self._last_readings[key].value)

            # Use cached readings for the data
            merged_readings = {k: self._last_readings.get(k) for k in readings.keys()}

            values = {k: (r.value if r is not None else None) for k, r in merged_readings.items()}
            cumulative_readings: dict[str, float | None] = {}
            now = datetime.now(timezone.utc)
            recheck = self._last_recent_check is None or (now - self._last_recent_check) > timedelta(hours=1)
            if recheck:
                self._last_recent_check = now
            for classifier in CUMULATIVE_CLASSIFIERS:
                await self._sync_recent_days(classifier, recheck)
                cumulative_readings[classifier] = (self._cumulative or {}).get(classifier, {}).get("cumulative")

            data: dict[str, Any] = {"readings": values, "cumulative_readings": cumulative_readings, "resources": self._resources, "costs": {}}

            elec = values.get("electricity.consumption")
            if elec is not None:
                elec_rate = self.tariff_config.get("electricity_rate", 0)
                elec_standing = self.tariff_config.get("electricity_standing_charge", 0)
                data["costs"]["electricity"] = round((elec * elec_rate) + elec_standing, 2)

            gas = values.get("gas.consumption")
            if gas is not None:
                gas_rate = self.tariff_config.get("gas_rate", 0)
                gas_standing = self.tariff_config.get("gas_standing_charge", 0)
                data["costs"]["gas"] = round((gas * gas_rate) + gas_standing, 2)

            data["costs"]["total"] = round(data["costs"].get("electricity", 0) + data["costs"].get("gas", 0), 2)
            data["costs"]["standing_charges_total"] = round(
                self.tariff_config.get("electricity_standing_charge", 0) +
                self.tariff_config.get("gas_standing_charge", 0), 2
            )

            return data

        except GlowmarktAuthError as err:
            raise UpdateFailed(f"Authentication error: {err}") from err
        except GlowmarktApiError as err:
            raise UpdateFailed(f"API error: {err}") from err

    @property
    def resources(self) -> dict[str, dict[str, Any]]:
        return self._resources

    def update_tariff_config(self, tariff_config: dict[str, float]) -> None:
        self.tariff_config = tariff_config

    def clear_daily_cache(self) -> None:
        """Clear the cached readings (call at midnight)."""
        self._last_readings.clear()
        _LOGGER.debug("Cleared daily reading cache")
