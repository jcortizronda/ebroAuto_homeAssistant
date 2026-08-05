"""Config flow de Ebro Auto — login por usuario con TELÉFONO + CONTRASEÑA (VERIFICADO 2026-07-27).

El login de Ebro usa teléfono+contraseña (grant_type=password) contra legend.ebroauto.com,
cliente legendApp:legendApp, tenant 3000010. Sin OTP/email. Tras el login se descubren tUserId
y VIN desde el backend. Las credenciales quedan en el config_entry del propio HA.

Flujo:
  1) user            → teléfono, contraseña, área (prefijo), PIN del vehículo → login directo
  2) select_vehicle  → (solo si la cuenta tiene varios vehículos) elección del VIN
  → crea la entrada. El token se renueva luego solo (refresh_token, < 12h).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol

from .const import (
    CONF_AREA_CODE,
    CONF_BFF,
    CONF_CAR_MQTT_HOST,
    CONF_CAR_MQTT_PORT,
    CONF_CERTS_SRC,
    CONF_CHANNEL_ID,
    CONF_PASSWORD,
    CONF_PHONE,
    CONF_PIN,
    CONF_PLUGGED_WAIT_MAX,
    CONF_POLL_CHARGING,
    CONF_POLL_MOVING,
    CONF_POLL_MOVING_IDLE,
    CONF_POLL_PARKED,
    CONF_POLL_PLUGGED,
    CONF_SIGN_KEY,
    CONF_TSP_HOST,
    CONF_TUSERID,
    CONF_VEHICLE_NAME,
    CONF_VIN,
    DEFAULT_AREA_CODE,
    DEFAULT_PLUGGED_WAIT_MAX_MIN,
    DEFAULT_POLL_CHARGING_MIN,
    DEFAULT_POLL_MOVING_IDLE_MIN,
    DEFAULT_POLL_MOVING_MIN,
    DEFAULT_POLL_PARKED_MIN,
    DEFAULT_POLL_PLUGGED_MIN,
    DEFAULTS,
    DOMAIN,
)
from .vehicle.config import VehicleConfig, build_ctx

_LOGGER = logging.getLogger(__name__)

_CORE = os.path.join(os.path.dirname(__file__), "core")


def _pending_token_path(hass: HomeAssistant) -> str:
    return hass.config.path(f"{DOMAIN}_pending_token.json")


def _reason_line(detail: str | None) -> str:
    detail = (detail or "").strip()
    return f"\n\n⚠️ Motivo: {detail}" if detail else ""


def _flow_ctx(hass: HomeAssistant, data: dict, token_path: str | None = None):
    """`CoreCtx` para los pasos del config flow.

    Misma fábrica que en runtime (`vehicle_config.build_ctx`): eran dos funciones que rellenaban
    los mismos doce campos y podían divergir. Lo único propio del alta es la ruta del token, que
    apunta al archivo «pendiente» hasta que se conoce el VIN definitivo."""
    return build_ctx(
        VehicleConfig.from_flow_data(data),
        token_path=token_path or _pending_token_path(hass),
    )


def _password_login(hass: HomeAssistant, data: dict) -> tuple[bool, str]:
    """Login teléfono+contraseña → guarda el token (raw) en la ruta pendiente. (ok, detalle)."""
    from .core import ebro_login as EL
    phone = (data.get(CONF_PHONE) or "").strip().lstrip("+").replace(" ", "")
    area = (data.get(CONF_AREA_CODE) or DEFAULT_AREA_CODE).strip().lstrip("+")
    ok, res = EL.password_login(phone, data.get(CONF_PASSWORD, ""), area_code=area)
    if not ok:
        return False, str(res)
    try:
        pend = _pending_token_path(hass)
        with open(pend, "w", encoding="utf-8") as fh:
            json.dump(res["raw"], fh, ensure_ascii=False)
        os.chmod(pend, 0o600)
    except OSError as e:
        return False, f"no puedo guardar el token: {e}"
    return True, "ok"


def _discover(hass: HomeAssistant, data: dict) -> tuple[bool, str, list[str], str]:
    """Tras el login: descubre (tUserId, [VIN]) del token recién generado. Solo lectura.

    La llamada a `queryList` y el parseo de su respuesta viven en `core/vehicles`, compartidos
    con el coordinator: eran dos copias con criterios distintos sobre bajo qué clave viene la
    lista de vehículos."""
    try:
        from .core import vehicles, wake

        ctx = _flow_ctx(hass, data, token_path=_pending_token_path(hass))
        _ut, tu = wake._bff_login(ctx)
        if not tu:
            return False, "", [], "login del backend fallido"
        respuesta = vehicles.query_list(
            ctx, {"tUserId": str(tu), "channelId": ctx.channel_id})
        encontrados = vehicles.vins(respuesta)
        return True, str(tu), encontrados, ("ok" if encontrados else "ningún vehículo encontrado")
    except Exception as e:
        return False, "", [], f"error al descubrir vehículos: {type(e).__name__}"


def _finalize_token(hass: HomeAssistant, vin: str) -> bool:
    pend = _pending_token_path(hass)
    dest = hass.config.path(f"{DOMAIN}_{vin}_token.json")
    try:
        if os.path.isfile(pend):
            os.replace(pend, dest)
        return os.path.isfile(dest)
    except OSError as e:
        _LOGGER.error("Ebro: no se pudo mover el token a %s: %s", dest, e)
        return False


def _cleanup_pending(hass: HomeAssistant) -> None:
    pend = _pending_token_path(hass)
    try:
        if os.path.isfile(pend):
            os.remove(pend)
    except OSError as e:
        _LOGGER.debug("Ebro: limpieza del token pendiente fallida: %s", e)


def _poll_intervals_schema(src: dict) -> dict:
    """Campos de los 5 intervalos de sondeo + el tope de "enchufado sin cargar", con los valores
    por defecto tomados de `src` (o de los DEFAULT_* si no están). Todo en minutos, 0 = off/sin
    límite. Compartido por el alta (config flow) y las opciones (options flow)."""
    def _rng():
        return vol.All(vol.Coerce(int), vol.Range(min=0, max=1440))
    return {
        vol.Optional(CONF_POLL_PARKED,
                     default=src.get(CONF_POLL_PARKED, DEFAULT_POLL_PARKED_MIN)): _rng(),
        vol.Optional(CONF_POLL_PLUGGED,
                     default=src.get(CONF_POLL_PLUGGED, DEFAULT_POLL_PLUGGED_MIN)): _rng(),
        vol.Optional(CONF_POLL_CHARGING,
                     default=src.get(CONF_POLL_CHARGING, DEFAULT_POLL_CHARGING_MIN)): _rng(),
        vol.Optional(CONF_POLL_MOVING,
                     default=src.get(CONF_POLL_MOVING, DEFAULT_POLL_MOVING_MIN)): _rng(),
        vol.Optional(CONF_POLL_MOVING_IDLE,
                     default=src.get(CONF_POLL_MOVING_IDLE, DEFAULT_POLL_MOVING_IDLE_MIN)): _rng(),
        vol.Optional(CONF_PLUGGED_WAIT_MAX,
                     default=src.get(CONF_PLUGGED_WAIT_MAX, DEFAULT_PLUGGED_WAIT_MAX_MIN)): _rng(),
    }


class EbroConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow de la integración (teléfono + contraseña)."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._tuserid: str = ""
        self._vins: list[str] = []
        self._vin_pending: str = ""

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> EbroOptionsFlow:
        return EbroOptionsFlow(config_entry)

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        reason = ""
        if user_input is not None:
            self._data.update(user_input)
            ok, msg = await self.hass.async_add_executor_job(
                _password_login, self.hass, self._data
            )
            if ok:
                d_ok, tu, vins, detail = await self.hass.async_add_executor_job(
                    _discover, self.hass, self._data
                )
                if d_ok and vins:
                    self._tuserid = tu
                    self._vins = vins
                    if len(vins) == 1:
                        self._vin_pending = vins[0]
                        return await self.async_step_poll()
                    return await self.async_step_select_vehicle()
                await self.hass.async_add_executor_job(_cleanup_pending, self.hass)
                errors["base"] = "no_vehicle"
                reason = _reason_line(detail)
                _LOGGER.warning("Ebro: detección del vehículo fallida: %s", detail)
            else:
                await self.hass.async_add_executor_job(_cleanup_pending, self.hass)
                errors["base"] = "login_failed"
                reason = _reason_line(msg)
                _LOGGER.warning("Ebro: acceso fallido: %s", msg)

        # Campos normales: lo único que necesita un usuario de Ebro EU.
        fields: dict[Any, Any] = {
            vol.Required(CONF_PHONE): str,
            vol.Required(CONF_PASSWORD): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)),
            vol.Required(CONF_PIN): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)),
            vol.Optional(CONF_AREA_CODE, default=DEFAULT_AREA_CODE): str,
        }
        # Ajustes internos (hosts, puerto MQTT, channel id, carpeta de certificados). Son fijos
        # para Ebro EU y el coordinador los rellena solos desde DEFAULTS, así que solo se muestran
        # con el «Modo avanzado» del perfil de HA activado (usuarios/regiones especiales).
        if self.show_advanced_options:
            fields.update({
                vol.Optional(CONF_BFF, default=DEFAULTS[CONF_BFF]): str,
                vol.Optional(CONF_TSP_HOST, default=DEFAULTS[CONF_TSP_HOST]): str,
                vol.Optional(CONF_CAR_MQTT_HOST, default=DEFAULTS[CONF_CAR_MQTT_HOST]): str,
                vol.Optional(CONF_CAR_MQTT_PORT,
                             default=DEFAULTS[CONF_CAR_MQTT_PORT]): vol.Coerce(int),
                vol.Optional(CONF_CHANNEL_ID, default=DEFAULTS[CONF_CHANNEL_ID]): str,
                vol.Optional(CONF_CERTS_SRC, default=""): str,
            })
        return self.async_show_form(step_id="user", data_schema=vol.Schema(fields), errors=errors,
                                    description_placeholders={"reason": reason})

    async def async_step_select_vehicle(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._vin_pending = user_input[CONF_VIN]
            return await self.async_step_poll()
        schema = vol.Schema({vol.Required(CONF_VIN): vol.In(self._vins)})
        return self.async_show_form(step_id="select_vehicle", data_schema=schema)

    async def async_step_poll(self, user_input: dict[str, Any] | None = None):
        """Segundo paso del alta: intervalos de sondeo (con valores por defecto, editables)."""
        if user_input is not None:
            return await self._create_entry(self._vin_pending, options=user_input)
        # comprueba duplicado ANTES de pedir los intervalos (no hacer rellenar en balde)
        await self._abort_if_duplicate(self._vin_pending)
        return self.async_show_form(
            step_id="poll", data_schema=vol.Schema(_poll_intervals_schema({})))

    async def _abort_if_duplicate(self, vin: str) -> None:
        """Aborta si ese VIN ya está configurado, limpiando antes el token pendiente.

        La limpieza es lo que hay que recordar: sin ella queda en la carpeta de configuración
        un `ebro_pending_token.json` con credenciales de un alta que no llegó a completarse."""
        await self.async_set_unique_id(vin)
        try:
            self._abort_if_unique_id_configured()
        except AbortFlow:
            await self.hass.async_add_executor_job(_cleanup_pending, self.hass)
            raise

    async def _create_entry(self, vin: str, options: dict | None = None):
        await self._abort_if_duplicate(vin)
        self._data[CONF_VIN] = vin
        self._data[CONF_TUSERID] = self._tuserid
        self._data[CONF_SIGN_KEY] = ""  # la HALF es constante en el código (tsp_sign.HALF)
        ok = await self.hass.async_add_executor_job(_finalize_token, self.hass, vin)
        if not ok:
            await self.hass.async_add_executor_job(_cleanup_pending, self.hass)
            return self.async_abort(reason="token_move_failed")
        return self.async_create_entry(
            title=f"Ebro Auto ({vin})", data=self._data, options=options or {})

    # ── Reconfiguración del PIN (sin desmontar la integración) ──
    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None):
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        errors: dict[str, str] = {}
        if entry is None:
            return self.async_abort(reason="reconfigure_no_entry")
        if user_input is not None:
            new_pin = (user_input.get(CONF_PIN) or "").strip()
            if not new_pin:
                errors["base"] = "pin_required"
            else:
                from homeassistant.helpers import issue_registry as ir
                ir.async_delete_issue(self.hass, DOMAIN, f"pin_wrong_{entry.entry_id}")
                coordinator = getattr(entry, "runtime_data", None)
                if coordinator is not None:
                    coordinator.ctx.reset_pin_lockout()
                return self.async_update_reload_and_abort(
                    entry, data={**entry.data, CONF_PIN: new_pin})
        schema = vol.Schema({
            vol.Required(CONF_PIN): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)),
        })
        return self.async_show_form(step_id="reconfigure", data_schema=schema, errors=errors)

    # ── Reautenticación: nuevo login teléfono+contraseña ──
    async def async_step_reauth(self, entry_data: dict[str, Any] | None = None):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None):
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        if entry is None:
            return self.async_abort(reason="reauth_no_entry")
        errors: dict[str, str] = {}
        reason = ""
        if user_input is not None:
            data = {**entry.data, **user_input}
            ok, msg = await self.hass.async_add_executor_job(
                _password_login, self.hass, data)
            if ok:
                # mueve el token pendiente a la ruta definitiva del VIN y recarga
                vin = entry.data.get(CONF_VIN, "")
                moved = await self.hass.async_add_executor_job(_finalize_token, self.hass, vin)
                if moved:
                    return self.async_update_reload_and_abort(entry, data=data)
                errors["base"] = "token_move_failed"
            else:
                await self.hass.async_add_executor_job(_cleanup_pending, self.hass)
                errors["base"] = "login_failed"
                reason = _reason_line(msg)
        schema = vol.Schema({
            vol.Required(CONF_PASSWORD): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)),
        })
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=schema, errors=errors,
            description_placeholders={"phone": entry.data.get(CONF_PHONE, ""), "reason": reason})


class EbroOptionsFlow(config_entries.OptionsFlow):
    """Opciones: los cinco intervalos (minutos) del sondeo realtime por estado + apodo. 0 = off."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(self, user_input: dict | None = None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        opt = self._entry.options or {}
        cur_name = opt.get(CONF_VEHICLE_NAME) or self._entry.data.get(CONF_VEHICLE_NAME) or ""
        schema = vol.Schema({
            **_poll_intervals_schema(opt),
            vol.Optional(CONF_VEHICLE_NAME,
                         description={"suggested_value": cur_name}): str,
        })
        return self.async_show_form(step_id="init", data_schema=schema)
