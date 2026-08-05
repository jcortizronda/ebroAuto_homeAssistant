"""Componente personalizado Ebro Auto — arranque.

La lógica MQTT/REST vive en `coordinator.py`, las entidades son nativas (nada de MQTT
Discovery). El "núcleo de protocolo" (auth, firma, comandos, sonda) se reutiliza desde
`core/` sin reescribir la lógica ya verificada sobre el terreno.
"""
from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import PLATFORMS
from .models import EbroConfigEntry

_LOGGER = logging.getLogger(__name__)

# El "núcleo de protocolo" es el subpaquete `.core`, importado con normalidad
# (`from .core import commands`). Con los imports de paquete es Python quien garantiza QUÉ
# módulo se carga, y la actualización de HACS invalida la caché por sí sola (los .pyc se
# indexan por ruta completa).
#
# Efecto secundario útil: los loggers de los módulos core/ ahora responden a `manifest.loggers`.


async def async_setup_entry(hass: HomeAssistant, entry: EbroConfigEntry) -> bool:
    """Inicializa la integración a partir de un config entry."""
    from .vehicle.coordinator import EbroCoordinator

    coordinator = EbroCoordinator(hass, entry)

    # FASE 3c: los certificados mutual-TLS deben existir ANTES de conectar el MQTT del coche.
    ok, detail = await coordinator.async_provision_certs()
    if not ok:
        raise ConfigEntryNotReady(detail)

    entry.runtime_data = coordinator

    # El monitor de diagnóstico se debe armar ANTES del primer control de sesión. Es ese
    # control el que decide si abrir la reautenticación, y es exactamente el evento que se
    # quiere releer tras un reinicio. Armándolo después (como estaba) el control de arranque
    # no se registraba nunca: verificado en 5 reinicios consecutivos, en el archivo de
    # diagnóstico quedaba un hueco justo en el momento más interesante.
    await coordinator.async_setup_diag()

    # estado inicial de sesión + inicio de la conexión MQTT al coche
    await coordinator.async_check_session()
    # [H4] si CUALQUIER paso del arranque falla (connect MQTT, inicio de timers, forward de
    #      las plataformas) limpiamos TODOS los recursos ya iniciados — cliente paho y
    #      timers keepalive/poll — y quitamos el coordinator de hass.data, para que no
    #      queden hilos/timers huérfanos; luego relanzamos → HA reintenta el setup.
    try:
        await coordinator.async_start()
        # keep-alive: refresco periódico de sesión para no dejar caducar el token en reposo
        coordinator.async_start_keepalive()
        # sondeo del canal realtime por estado (solo lectura). No hace nada si el interruptor
        # "Actualización automática" está apagado; el ritmo lo decide el estado del coche.
        coordinator.async_start_telemetry_poll()
        # recarga la entrada cuando el usuario cambia las opciones (p. ej. intervalos de sondeo)
        entry.async_on_unload(entry.add_update_listener(_async_options_updated))
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        # relleno de la identidad del vehículo (nombre de dispositivo dinámico) para las entradas
        # creadas antes de que el config flow la guardara: en segundo plano, para que una posible
        # recarga ocurra con el setup ya terminado.
        hass.async_create_background_task(
            coordinator.async_ensure_vehicle_identity(), "ebro_vehicle_identity")
    except Exception:
        await hass.async_add_executor_job(coordinator.async_stop)
        raise
    return True


async def _async_options_updated(hass: HomeAssistant, entry: EbroConfigEntry) -> None:
    """Recarga la entrada SOLO si de verdad han cambiado las opciones.

    `add_update_listener` salta en cada `async_update_entry`, incluso cuando lo que cambia es
    `entry.data` y no las opciones. Dos consecuencias, ambas reales:

    * el relleno de la identidad del vehículo (tarea en segundo plano lanzada por el setup)
      escribe en `entry.data` → la entrada recién iniciada se **recargaba de inmediato**. En el
      primer arranque HA la cargaba dos veces, y si el apagado llegaba mientras la recarga estaba
      en vuelo la tarea quedaba colgada más allá de la fase de cierre (HA lo avisa: «Integrations
      should cancel non-critical tasks … to prevent delaying shutdown») dejando la entrada en
      UNLOAD_IN_PROGRESS;
    * las rutas que cambian el PIN (Repair y reconfigurar) ya se recargan **por sí mismas** de
      forma explícita → el listener añadía una segunda recarga inútil.

    Quien de verdad necesita la recarga es solo el options flow (intervalos de sondeo, override
    del nombre), que no recarga por su cuenta. Se compara por tanto con la foto de las opciones
    aplicadas por el coordinator vivo. Si el coordinator no existe (entrada aún no en
    `hass.data`) se recarga, que es el comportamiento prudente de antes.

    Se usa `async_schedule_reload` en vez de esperar `async_reload`: el listener no se queda
    colgado esperando la recarga de sí mismo, y es HA quien posee y cancela la tarea al apagar.
    """
    # `runtime_data` no existe todavía si la entrada aún no ha terminado de cargar; en ese
    # caso se recarga, que es el comportamiento prudente de siempre.
    coordinator = getattr(entry, "runtime_data", None)
    if coordinator is not None and dict(entry.options or {}) == coordinator.applied_options:
        return
    hass.config_entries.async_schedule_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: EbroConfigEntry) -> bool:
    """Descarga la integración."""
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    # [MED] solo si la descarga de las plataformas tuvo éxito desmontamos el coordinator: si
    #       una plataforma rechaza la descarga (ok=False) HA considera la entrada aún cargada
    #       → no destruimos el coordinator bajo entidades todavía vivas (estado coherente; HA
    #       reintentará la descarga).
    if ok:
        # async_stop es bloqueante (loop_stop hace join del hilo paho) → executor.
        # `runtime_data` lo limpia Home Assistant al descargar: aquí ya no hay `pop`.
        await hass.async_add_executor_job(entry.runtime_data.async_stop)
    return ok
