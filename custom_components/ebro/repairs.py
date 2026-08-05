"""Repair flow de Ebro Auto — "PIN de comandos erróneo": reconfigura el PIN de 4 cifras de
los comandos remotos sin desmontar la integración.

El aviso lo crea el coordinator (`_raise_pin_issue`) cuando un comando falla porque el
backend rechaza el taskId (PIN erróneo / anti-bloqueo). El PIN NO sirve para el login →
corregirlo es pura escritura en entry.data + reload, seguido del reseteo explícito del
anti-bloqueo de ese vehículo (ver `_clear_pin_lockout`)."""
from __future__ import annotations

from typing import Any

from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol

from .const import CONF_PIN


def _clear_pin_lockout(hass: HomeAssistant, entry_id: str) -> None:
    """Pone a cero INCONDICIONALMENTE el anti-bloqueo + el taskId en caché del vehículo.

    El reset ocurre aunque el usuario reconfirme el MISMO PIN: el bloqueo podía no ser culpa
    del PIN, y sin reset los comandos seguirían fallando en silencio hasta que venza la
    ventana. Aquí el usuario ha hecho un gesto explícito de remedio → se parte siempre limpio.

    El estado es por vehículo (en el `CoreCtx` del coordinator), no un global compartido por
    todos los coches configurados."""
    entry = hass.config_entries.async_get_entry(entry_id)
    coordinator = getattr(entry, "runtime_data", None) if entry else None
    if coordinator is not None:
        coordinator.ctx.reset_pin_lockout()


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, Any] | None,
) -> RepairsFlow:
    """Factory que pide HA para el aviso `pin_wrong`."""
    return EbroPinRepairFlow(data or {})


class EbroPinRepairFlow(RepairsFlow):
    """Pide el nuevo PIN de comandos y lo aplica a la entrada (luego reload)."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._entry_id: str | None = data.get("entry_id")

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        return await self.async_step_pin()

    async def async_step_pin(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        entry = (
            self.hass.config_entries.async_get_entry(self._entry_id)
            if self._entry_id
            else None
        )
        if entry is None:
            return self.async_abort(reason="entry_not_found")
        errors: dict[str, str] = {}
        if user_input is not None:
            new_pin = (user_input.get(CONF_PIN) or "").strip()
            if not new_pin:
                errors["base"] = "pin_required"
            else:
                # escribe el nuevo PIN y recarga; al completar el fix flow se quita el aviso.
                self.hass.config_entries.async_update_entry(
                    entry, data={**entry.data, CONF_PIN: new_pin}
                )
                await self.hass.config_entries.async_reload(entry.entry_id)
                # reset incondicional, aunque el PIN reintroducido sea idéntico.
                # Tras el reload, para que actúe sobre el coordinator recreado.
                _clear_pin_lockout(self.hass, entry.entry_id)
                return self.async_create_entry(title="", data={})
        # campo CONTRASEÑA, sin valor por defecto con el PIN actual (credencial en claro en el form).
        schema = vol.Schema(
            {
                vol.Required(CONF_PIN): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                )
            }
        )
        return self.async_show_form(
            step_id="pin", data_schema=schema, errors=errors
        )
