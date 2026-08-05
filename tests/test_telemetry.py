"""Tests de `telemetry.py` — el mapa de la telemetría y el parseo de los mensajes MQTT.

Estos tests vivían en `test_coordinator.py` y necesitaban un coordinator (y por tanto un
`hass`) para ejercitar funciones que no dependen de ninguno de los dos. Ahora que la
interpretación del payload es pura, se prueban directamente: qué es una confirmación, qué es
un latido de marcha y qué campos son estado del vehículo se decide aquí, sin arrancar HA.
"""

from __future__ import annotations

import json

import pytest

from custom_components.ebro.const import MAX_STATUS_LEN
from custom_components.ebro.vehicle.telemetry import (
    CMD_CONFIRM_META,
    content_fingerprint,
    format_command_result,
    geo_only,
    is_unit_flag,
    parse_car_message,
    unknown_fields,
)


def _payload(service_type: str, data: dict) -> bytes:
    """Un mensaje MQTT del coche, con el envoltorio real."""
    return json.dumps({"content": {"serviceType": service_type, "data": data}}).encode()


# ───────────────────────── huella del contenido ─────────────────────────


def test_content_fingerprint_ignora_los_campos_reloj_a_cualquier_profundidad() -> None:
    """Sin esto, dos respuestas con datos idénticos parecerían siempre «cambiadas» y el
    sensor de frescura se movería en cada lectura."""
    con_relojes = {
        "dumpEnergy": "80",
        "resultTime": "2026-01-15T12:00:00",
        "anidado": {"collectTime": 1, "odometer": "1"},
        "lista": [{"time": 5, "v": 1}],
    }
    otros_relojes = {
        "dumpEnergy": "80",
        "resultTime": "2026-01-15T18:30:00",
        "anidado": {"collectTime": 99, "odometer": "1"},
        "lista": [{"time": 7, "v": 1}],
    }
    dato_distinto = {**con_relojes, "dumpEnergy": "79"}

    assert content_fingerprint(con_relojes) == content_fingerprint(otros_relojes)
    assert content_fingerprint(con_relojes) != content_fingerprint(dato_distinto)


def test_content_fingerprint_ignora_el_orden_de_las_claves() -> None:
    assert content_fingerprint({"a": 1, "b": 2}) == content_fingerprint({"b": 2, "a": 1})


# ───────────────────────── flags de unidad ─────────────────────────


@pytest.mark.parametrize(
    ("key", "es_unidad"),
    [
        ("rangeUnit", True),
        ("averageFuelUnit", True),
        ("tirePressureUnit", True),
        ("dumpEnergy", False),
        ("unitario", False),
    ],
)
def test_is_unit_flag(key: str, es_unidad: bool) -> None:
    """Los `*Unit` valen siempre 1 o 2 y solo dicen EN QUÉ unidad viene el campo homónimo.

    Filtrarlos evita que el auto-descubrimiento de campos los señale como «por mapear»: ruido
    puro que escondía los campos realmente nuevos.
    """
    assert is_unit_flag(key) is es_unidad


# ───────────────────────── resultado de un comando ─────────────────────────


@pytest.mark.parametrize(
    ("data", "esperado"),
    [
        ({"reason": ["puerta abierta"]}, "El coche ha señalado un problema ❌"),
        ({"result": "5"}, "Comando en ejecución en el coche… ⏳"),
        ({"result": "1"}, "Comando ejecutado y confirmado por el coche ✅"),
        ({"result": "2"}, "Comando ejecutado y confirmado por el coche ✅"),
        ({"result": "99"}, "Confirmación recibida del coche (código 99)"),
        ({}, "Confirmación recibida del coche (código ?)"),
        # `reason` gana sobre un `result` de éxito: si el coche señala un motivo, falló
        ({"result": "1", "reason": ["fallo"]}, "El coche ha señalado un problema ❌"),
    ],
)
def test_format_command_result(data: dict, esperado: str) -> None:
    assert format_command_result(data).startswith(esperado)


def test_format_command_result_se_recorta() -> None:
    """El valor acaba en el estado de un sensor, que HA limita en longitud."""
    assert len(format_command_result({"reason": ["x" * 500]})) <= MAX_STATUS_LEN


# ───────────────────────── geolocalización ─────────────────────────


def test_geo_only_deja_fuera_lo_que_no_es_posicion() -> None:
    """Batería y estado del cable viven en `realtime`, no en la posición: guardarlos aquí
    (con `**data`) los hacía viajar al device_tracker."""
    assert geo_only({"lat": "40.4", "lon": "-3.7", "dumpEnergy": "80", "chargeGunState": "1"}) == {
        "lat": "40.4",
        "lon": "-3.7",
    }


# ───────────────────────── parseo del mensaje ─────────────────────────


def test_parse_car_message_payload_ilegible_no_lanza() -> None:
    """El parseo corre en el hilo de paho: una excepción ahí se lleva el hilo por delante."""
    assert parse_car_message(b"esto no es json") is None
    assert parse_car_message(b"") is None


def test_parse_car_message_separa_estado_de_metadatos_de_confirmacion() -> None:
    """Un push de confirmación trae los campos de estado REALES además de los metadatos del
    comando. Los primeros van a `fields`; los segundos no son estado del vehículo."""
    message = parse_car_message(
        _payload("1105", {"result": "1", "seq": "VIN-123", "hasAsy": "0", "doorLock": "1"})
    )

    assert message.is_confirmation is True
    assert message.state_fields == {"doorLock": "1"}
    for meta in CMD_CONFIRM_META:
        assert meta not in message.state_fields


def test_parse_car_message_telemetria_pura_no_es_confirmacion() -> None:
    message = parse_car_message(_payload("5A02", {"doorLock": "0", "hood": "0"}))

    assert message.is_confirmation is False
    assert message.meaningful is True
    assert message.state_fields == {"doorLock": "0", "hood": "0"}


def test_parse_car_message_latido_de_marcha_no_es_significativo() -> None:
    """Solo `time` = latido que el coche emite cada pocos segundos mientras circula. No debe
    mover «Último contacto» ni escribir en el recorder durante todo el viaje."""
    message = parse_car_message(_payload("5A02", {"time": "1737000000000"}))

    assert message.meaningful is False
    assert message.state_fields == {}


def test_parse_car_message_posicion_se_discrimina_por_tipo_de_mensaje() -> None:
    """Un 5A02 con lat/lon NO es un reporte de posición: el tipo es lo que manda."""
    posicion = parse_car_message(_payload("1301", {"lat": "40.4", "lon": "-3.7"}))
    telemetria = parse_car_message(_payload("5A02", {"lat": "40.4", "lon": "-3.7"}))

    assert posicion.geo == {"lat": "40.4", "lon": "-3.7"}
    assert telemetria.geo == {}


def test_parse_car_message_acepta_el_payload_sin_envoltorio() -> None:
    """Algunos mensajes vienen ya como contenido, sin la envoltura `content`."""
    crudo = json.dumps({"serviceType": "5A02", "data": {"hood": "1"}}).encode()

    assert parse_car_message(crudo).state_fields == {"hood": "1"}


# ───────────────────────── auto-descubrimiento ─────────────────────────


def test_unknown_fields_señala_solo_lo_que_no_esta_mapeado() -> None:
    message = parse_car_message(
        _payload("5A02", {"doorLock": "0", "campoNuevo": "7", "rangeUnit": "1"})
    )

    assert unknown_fields(message) == [("campoNuevo", "7")]


def test_unknown_fields_nunca_registra_una_coordenada() -> None:
    """Fuga real vista en campo el 2026-07-20: comparar un push de POSICIÓN con `META` hacía
    que lat/lon se registraran como «campos por mapear» en el archivo de diagnóstico."""
    posicion = parse_car_message(_payload("1301", {"lat": "40.4", "lon": "-3.7"}))
    telemetria_con_geo = parse_car_message(_payload("5A02", {"lat": "40.4", "lon": "-3.7"}))

    assert unknown_fields(posicion) == []
    assert unknown_fields(telemetria_con_geo) == []
