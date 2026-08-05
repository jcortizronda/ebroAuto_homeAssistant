"""Entidad base de Ebro Auto: enganche al coordinator + device_info común.

Continuidad del entity_id: cada entidad FIJA su propio `entity_id` en vez de dejarlo
derivar de forma implícita. El object_id por defecto = slugify(nombre). Donde hace falta
un id no derivable del nombre (p. ej. los botones de comando = `ebro_<key>`) se pasa un
`object_id` explícito.
"""
from __future__ import annotations

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .const import DEFAULT_VEHICLE_NAME, DOMAIN
from .helpers import field_on
from .vehicle.coordinator import EbroCoordinator

# `field_on` vive ahora en `helpers.py` (junto a las demás conversiones sobre los datos del
# coche) y se reexporta aquí: es el nombre por el que lo importan binary_sensor, switch,
# cover, lock, climate y el coordinator.
__all__ = ["EbroEntity", "EbroOptimisticMixin", "EbroRestoreStateMixin", "field_on"]


class EbroEntity(CoordinatorEntity[EbroCoordinator]):
    """Entidad base: dispositivo único 'Ebro Auto' identificado por el VIN."""

    # has_entity_name=True + translation_key → el NOMBRE de la entidad se TRADUCE y HA lo
    # antepone al dispositivo → "Ebro Auto Batería". El entity_id se fija explícitamente abajo
    # (a partir de `name`/`object_id`).
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EbroCoordinator,
        name: str,
        unique_suffix: str,
        *,
        object_id: str | None = None,
        entity_id_format: str | None = None,
    ) -> None:
        super().__init__(coordinator)
        # `name` NO es el friendly name (lo da translation_key): lo mantenemos solo para
        # calcular el object_id del entity_id y para los logs. NO fijar _attr_name, o ganaría
        # sobre el translation_key.
        self._raw_name = name
        self._attr_unique_id = f"{coordinator.vin}_{unique_suffix}"
        oid = object_id or slugify(name)          # p. ej. "ebro_bateria"
        # translation_key = object_id sin el prefijo del dominio → clave en translations/*.json
        # (el NOMBRE visible sale de aquí: NO se toca, así queda el castellano de es.json).
        key = oid[len(DOMAIN) + 1:] if oid.startswith(f"{DOMAIN}_") else oid
        self._attr_translation_key = key
        # entity_id = <plataforma>.ebro_<últimas 4 del VIN>_<translation_key>. Las 4 cifras del
        # VIN lo hacen único incluso con varios vehículos en el mismo Home Assistant.
        #
        # Antes el descriptor se sacaba del nombre traducido de `translations/es.json`, leído en
        # runtime y cacheado en un `lru_cache` global de proceso, precalentado en executor desde
        # `async_setup_entry` (o HA marcaba el `open()` como blocking call en el loop) y vaciado
        # por una fixture autouse del conftest para que el orden de los tests no contaminara los
        # snapshots. Todo ese aparato re-derivaba lo que la clave ya contiene: `name` es
        # literalmente "Ebro " + el nombre castellano, así que `slugify(nombre_es)` y la
        # `translation_key` coinciden. Verificado contra las 93 entidades de los snapshots
        # versionados: coincidían en las 93.
        if entity_id_format:
            vin4 = (coordinator.vin or "")[-4:]
            self.entity_id = entity_id_format.format(f"{DOMAIN}_{vin4}_{key}")
        # dispositivo dinámico: el nombre refleja el vehículo real (Ebro Auto, Ebro S700…), leído
        # del coordinator (apodo/modelo de queryList, o override manual). El dispositivo se
        # identifica por el VIN → renombrarlo NO toca el entity_id ni el historial.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.vin)},
            name=coordinator.vehicle_name or DEFAULT_VEHICLE_NAME,
            manufacturer=coordinator.vehicle_brand or "Ebro",
            model=coordinator.vehicle_model or None,
        )


class EbroRestoreStateMixin:
    """Restaura el último estado on/off conocido al reiniciar Home Assistant.

    El estado real del coche (telemetría 5A02) vive en memoria en el coordinator: tras un
    reinicio de HA vuelve a `unknown` y solo se repuebla cuando el coche despierta y publica
    — pueden pasar horas. Mientras tanto se muestra el último valor conocido.

    Diez clases repetían este mismo bloque de seis líneas, cada una con su pareja de estados
    (`on`/`off`, `locked`/`unlocked`, `open`/`closed`). Aquí la pareja es un atributo de clase
    y el resto es común. Usar como PRIMERA clase base junto a `RestoreEntity`.
    """

    #: (estado verdadero, estado falso) tal como Home Assistant los persiste.
    _restore_states: tuple[str, str] = ("on", "off")
    _restored: bool | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in self._restore_states:
            self._restored = last.state == self._restore_states[0]

    def _restored_or(self, live: bool | None) -> bool | None:
        """El valor en vivo, o el restaurado mientras el coche no haya dicho nada."""
        return live if live is not None else self._restored


class EbroOptimisticMixin:
    """Estado optimista para los actuadores (lock/switch/cover).

    Un comando ACTÚA de inmediato sobre el coche, pero el estado real vuelve SOLO por MQTT con
    el coche despierto: el último valor "en vivo" puede quedarse quieto durante horas. Tras una
    acción mostramos de inmediato el estado objetivo (optimista) y lo mantenemos hasta que
    llega un NUEVO mensaje del coche (avanza `last_seen`), que pasa a ser la verdad.
    Usar como PRIMERA clase base (precede a EbroEntity en el MRO)."""

    _opt_value = None
    _opt_anchor = None

    def _set_optimistic(self, value) -> None:
        self._opt_value = value
        self._opt_anchor = self.coordinator.data.get("last_seen")
        self.async_write_ha_state()

    def _clear_optimistic(self) -> None:
        self._opt_value = None
        self._opt_anchor = None

    def _optimistic_or(self, value):
        """El objetivo optimista si hay uno en vigor, si no el valor que se le pasa."""
        return self._opt_value if self._opt_value is not None else value

    def _resolved(self, live):
        """El estado a mostrar: optimista → en vivo → restaurado.

        Es el orden de prioridad que comparten TODOS los actuadores, y estaba escrito a mano
        como `self._optimistic_or(self._restored_or(live))` en cinco clases. Requiere que la
        entidad use también `EbroRestoreStateMixin`, que es el caso de las cinco."""
        return self._optimistic_or(self._restored_or(live))

    def _command_error(self, key: str, err: Exception) -> HomeAssistantError:
        """Error legible cuando el comando falla. Las subclases con un dominio propio
        (p. ej. `climate`) lo redefinen para hablar de su función y no de la clave interna."""
        return HomeAssistantError(f"Comando «{key}» fallido: {err}")

    async def _run_command(self, key: str, target, params: dict | None = None) -> None:
        """Ejecuta un comando mostrando de inmediato el estado objetivo (optimista).

        `params` = override paramétrico del body (clima: temperatura/duración; carga
        programada: plan). Ante una excepción del comando (red/auth/backend) ANULA el
        optimismo — así la card vuelve al estado real en vez de quedarse bloqueada en un
        objetivo nunca ejecutado — y propaga un error legible (toast en la UI).

        [cola] El coche ejecuta UN comando cada vez: un segundo comando (o un doble toque) no
        se rechaza sino que ESPERA su turno en la cola del coordinator, que lo envía en cuanto
        el coche ha confirmado el anterior."""
        self._set_optimistic(target)
        try:
            await self.coordinator.async_send_command(key, params)
        except Exception as err:
            self._clear_optimistic()
            self.async_write_ha_state()
            raise self._command_error(key, err) from err

    def _handle_coordinator_update(self) -> None:
        # un nuevo mensaje del coche (last_seen cambiado) invalida el optimismo
        if self._opt_value is not None and \
                self.coordinator.data.get("last_seen") != self._opt_anchor:
            self._clear_optimistic()
        super()._handle_coordinator_update()
