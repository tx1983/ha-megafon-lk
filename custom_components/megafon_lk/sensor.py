"""Sensors: balance, remainders, tariff."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfInformation, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import MegafonConfigEntry, MegafonCoordinator

RUB = "RUB"


def _to_float(value: Any) -> float | None:
    """Tariff prices come as strings ("950"), balance as numbers."""
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _parse_charge_date(value: Any) -> datetime | None:
    """'24.11.2026 20:25' -> aware datetime.

    NOTE: assumes the time is in the HA time zone; MegaFon probably uses the
    subscriber's region time. Verify against the real charge time.
    """
    try:
        naive = datetime.strptime(value, "%d.%m.%Y %H:%M")
    except (TypeError, ValueError):
        return None
    return naive.replace(tzinfo=dt_util.get_default_time_zone())


def _remainder(data: dict[str, Any], kind: str) -> dict[str, Any]:
    return data["remainders"].get(kind) or {}


def _remainder_value(kind: str) -> Callable[[dict[str, Any]], StateType]:
    def _fn(data: dict[str, Any]) -> StateType:
        return _remainder(data, kind).get("availableValue", {}).get("value")

    return _fn


def _remainder_attrs(kind: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def _fn(data: dict[str, Any]) -> dict[str, Any]:
        rem = _remainder(data, kind)
        return {
            "total": rem.get("totalValue", {}).get("value"),
            "percent_left": rem.get("interestValue", {}).get("value"),
            "unlimited": rem.get("unlim"),
        }

    return _fn


def _charges(data: dict[str, Any]) -> dict[str, Any]:
    return data["tariff"].get("ratePlanCharges") or {}


@dataclass(frozen=True, kw_only=True)
class MegafonSensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], StateType | datetime]
    attrs_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None


SENSORS: tuple[MegafonSensorDescription, ...] = (
    MegafonSensorDescription(
        key="balance",
        translation_key="balance",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=RUB,
        suggested_display_precision=2,
        value_fn=lambda d: d["balance"].get("balance"),
    ),
    MegafonSensorDescription(
        key="cashback",
        translation_key="cashback",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=RUB,
        suggested_display_precision=2,
        value_fn=lambda d: d["balance"].get("cashback"),
    ),
    MegafonSensorDescription(
        key="minutes",
        translation_key="minutes",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_remainder_value("VOICE"),
        attrs_fn=_remainder_attrs("VOICE"),
    ),
    MegafonSensorDescription(
        key="internet",
        translation_key="internet",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.GIGABYTES,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=_remainder_value("INTERNET"),
        attrs_fn=_remainder_attrs("INTERNET"),
    ),
    MegafonSensorDescription(
        key="sms",
        translation_key="sms",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_remainder_value("MESSAGE"),
        attrs_fn=_remainder_attrs("MESSAGE"),
    ),
    MegafonSensorDescription(
        key="tariff",
        translation_key="tariff",
        value_fn=lambda d: d["tariff"].get("name"),
        attrs_fn=lambda d: {
            "start_date": d["tariff"].get("startDate"),
            "tariff_id": d["tariff"].get("id"),
        },
    ),
    MegafonSensorDescription(
        key="tariff_price",
        translation_key="tariff_price",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=RUB,
        suggested_display_precision=0,
        value_fn=lambda d: _to_float(
            _charges(d).get("totalMonthlyPrice", {}).get("value")
        ),
        attrs_fn=lambda d: {
            c.get("type"): _to_float(c.get("price", {}).get("value"))
            for c in _charges(d).get("componentPrice", [])
        },
    ),
    MegafonSensorDescription(
        key="next_charge_date",
        translation_key="next_charge_date",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda d: _parse_charge_date(
            _charges(d).get("nextCharge", {}).get("chargeDate")
        ),
    ),
    MegafonSensorDescription(
        key="next_charge_amount",
        translation_key="next_charge_amount",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=RUB,
        suggested_display_precision=0,
        value_fn=lambda d: _to_float(
            _charges(d).get("nextCharge", {}).get("price", {}).get("value")
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MegafonConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    unique_id = entry.unique_id or entry.entry_id
    async_add_entities(
        MegafonSensor(coordinator, description, unique_id) for description in SENSORS
    )


class MegafonSensor(CoordinatorEntity[MegafonCoordinator], SensorEntity):
    entity_description: MegafonSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: MegafonCoordinator,
        description: MegafonSensorDescription,
        unique_id: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{unique_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, unique_id)},
            name=f"МегаФон …{unique_id[-4:]}",
            manufacturer="МегаФон",
        )

    @property
    def native_value(self) -> StateType | datetime:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if fn := self.entity_description.attrs_fn:
            return fn(self.coordinator.data)
        return None
