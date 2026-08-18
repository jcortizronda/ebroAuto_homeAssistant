"""Diagnóstico descargable de la integración Ebro Auto.

Genera el informe que HA ofrece con «Descargar diagnóstico» en la página de la
integración. Pensado para el SOPORTE: contiene estado de sesión, parámetros de región,
presencia de token/certificados y la última telemetría recibida, pero NO expone ningún dato
personal o secreto:

  • email, PIN, VIN, tUserId            → ocultados (REDACTED)
  • posición GPS (lat/lon)              → ocultada (dónde vives no sale nunca)
  • token y certificados mutual-TLS     → solo «presente: sí/no», nunca el contenido

Así el usuario puede enviarte el archivo con total seguridad.
"""
from __future__ import annotations

import os
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CERT_FILES
from .helpers import fields
from .models import EbroConfigEntry

# Claves a ocultar dondequiera que aparezcan (config entry + posibles dicts anidados).
# NB: «seq» está aquí porque en el payload realtime vale "<VIN>-<timestamp>" → contiene el VIN.
# NB: «certs_src» es la RUTA desde la que el usuario importó los certificados mutual-TLS: es
# revelación de info del filesystem (nombre de usuario, estructura de carpetas, a veces un
# backup de la app) y no sirve al soporte → ocultada.
# NB: «password» y «phone» son las CREDENCIALES DE LA CUENTA, guardadas en claro en
# entry.data (ver config_flow._create_entry). `async_redact_data` NO oculta nada por su
# cuenta: solo las claves que se le pasan → sin ellas aquí, la contraseña de la cuenta Ebro
# y el teléfono de acceso salían EN CLARO en el archivo que la cabecera de este módulo
# promete «seguro de enviar». El teléfono sustituyó al email como identificador de acceso
# (2026-07-27) y la cabecera ya prometía ocultar el email.
TO_REDACT = {
    "email", "phone", "password", "pin", "vin", "tuserid", "seq", "certs_src",
    "lat", "lon", "latitude", "longitude", "position",
}


def _scrub_vin(obj: Any, vin: str) -> Any:
    """Red de seguridad: quita el VIN dondequiera que aparezca como SUBCADENA, incluso dentro
    de un campo que la ocultación por clave no conoce (ej. un id compuesto)."""
    if not vin:
        return obj
    if isinstance(obj, dict):
        return {k: _scrub_vin(v, vin) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub_vin(v, vin) for v in obj]
    if isinstance(obj, str) and vin in obj:
        return obj.replace(vin, "**REDACTED**")
    return obj


def _scrub_geo(obj: Any) -> Any:
    """Quita las coordenadas dondequiera que aparezcan DENTRO DE UNA CADENA (2026-07-20).

    `TO_REDACT` cubre `lat`/`lon` cuando son claves de un diccionario. Pero no cubría el
    caso real encontrado en campo: `probe_status` es un mensaje discursivo destinado al
    usuario y contenía «lat=40.90…, lon=14.34…» — las coordenadas del coche acababan por
    tanto en claro justo en el archivo que la cabecera de este módulo promete «seguro de
    enviar».

    Es el mismo defecto ya corregido en el monitor de diagnóstico, y se reutiliza a propósito
    el MISMO patrón: dos implementaciones separadas de la misma regla divergen, y la segunda
    copia sería la olvidada.
    """
    from .vehicle.diag import scrub_coordinates  # solo stdlib, import seguro

    if isinstance(obj, dict):
        return {k: _scrub_geo(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub_geo(v) for v in obj]
    if isinstance(obj, str):
        return scrub_coordinates(obj)
    return obj


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: EbroConfigEntry
) -> dict[str, Any]:
    """Informe de diagnóstico para un config entry (invocado por «Descargar diagnóstico»)."""
    diag: dict[str, Any] = {
        "entry": {
            "version": entry.version,
            # título forzado sin VIN (el título real es "Ebro Auto (<VIN>)")
            "title": "Ebro Auto",
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            # también las options pasan por la ocultación: hoy solo contienen intervalos de
            # sondeo, pero así una clave sensible añadida mañana ya queda cubierta.
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
    }

    coordinator = getattr(entry, "runtime_data", None)
    if coordinator is None:
        diag["coordinator"] = "no inicializado (entrada no cargada)"
        return diag

    # Presencia de los archivos sensibles como simples booleanos — nunca su contenido.
    token_present = await hass.async_add_executor_job(
        os.path.isfile, coordinator.token_path
    )
    certs_present: dict[str, bool] = {}
    for fname in CERT_FILES:
        path = os.path.join(coordinator.certs_dir, fname)
        certs_present[fname] = await hass.async_add_executor_job(os.path.isfile, path)

    data = dict(coordinator.data or {})
    has_position = bool(data.get("position"))
    vin = getattr(coordinator, "vin", "") or ""
    # La posición GPS es sensible (dónde vives) → nunca exportada, ni ocultada coord a coord.
    realtime = data.get("realtime")
    if isinstance(realtime, dict):
        realtime = _scrub_vin(async_redact_data(realtime, TO_REDACT), vin)
    # Telemetría 5A02 (estado puertas/clima/asientos…): ocultación por clave + pasada anti-VIN.
    telemetry_fields = fields(data)
    if isinstance(telemetry_fields, dict):
        telemetry_fields = _scrub_vin(
            async_redact_data(dict(telemetry_fields), TO_REDACT), vin)

    diag["coordinator"] = {
        "region": {
            "bff": coordinator.bff,
            "tsp_host": coordinator.tsp_host,
            "car_mqtt_host": coordinator.car_host,
            "car_mqtt_port": coordinator.car_port,
            "channel_id": coordinator.channel_id,
        },
        "poll": {
            "parked_min": coordinator.poll_parked_min,
            "plugged_min": coordinator.poll_plugged_min,
            "charging_min": coordinator.poll_charging_min,
            "moving_min": coordinator.poll_moving_min,
            "moving_idle_min": coordinator.poll_moving_idle_min,
            "plugged_wait_max_s": coordinator.plugged_wait_max_s,
            "enabled": coordinator.poll_enabled,
        },
        "token_present": token_present,
        "certs_present": certs_present,
        "state": {
            "session_ok": data.get("session_ok"),
            "session_detail": data.get("session_detail"),
            "awake": data.get("awake"),
            "car_connected": data.get("car_connected"),
            # conectado NO es lo mismo que suscrito: el broker puede aceptar la conexión y
            # denegar el topic, y entonces la telemetría no llega nunca.
            "car_subscribed": data.get("car_subscribed"),
            "car_subscribe_detail": data.get("car_subscribe_detail"),
            "has_position_fix": has_position,
            "last_seen": data.get("last_seen"),
            "last_wake": data.get("last_wake"),
            "last_pos_fix": data.get("last_pos_fix"),
            "cmd_status": data.get("cmd_status"),
            "wake_status": data.get("wake_status"),
            "probe_status": data.get("probe_status"),
            "realtime": realtime,
            "fields_count": len(fields(data)),
            "fields": telemetry_fields,
        },
    }

    # Monitor de diagnóstico (diag.py), presente solo si está activo: ring buffer + contadores.
    # Los eventos ya están ocultados en la captura; aquí pasan igualmente por la ocultación
    # estándar — defensa en profundidad, como para realtime/fields arriba.
    recorder = coordinator.diag_recorder
    if recorder is not None:
        snap = recorder.snapshot()
        diag["diagnostic_mode"] = _scrub_vin(async_redact_data(snap, TO_REDACT), vin)

    # Pasada FINAL sobre las coordenadas, en todo el informe (2026-07-20).
    # Está aquí, al final y una sola vez, a propósito: aplicarla a los campos individuales
    # significaría acordarse de hacerlo para cada uno — y es exactamente el olvido que hizo
    # salir la posición dentro de `probe_status`, un mensaje discursivo que ninguna deny-list
    # por clave podía cubrir. Aquí no hay nada que recordar: lo que sale del módulo ya ha
    # pasado por aquí.
    return _scrub_geo(diag)
