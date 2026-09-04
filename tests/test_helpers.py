"""Tests de `helpers.py` — las conversiones compartidas sobre los datos del coche.

Son funciones puras: ni `hass`, ni red, ni fixtures. Merecen test propio porque el criterio
que aplican («campo ausente ≠ cero») es justo el que se torcía cuando estaba copiado en
catorce sitios: bastaba que una copia devolviera `0.0` en vez de `None` para que la batería
apareciera al 0 % o el odómetro se reiniciara al recibir un frame incompleto.
"""

from __future__ import annotations

import pytest

from custom_components.ebro.const import MAX_STATUS_LEN
from custom_components.ebro.helpers import (
    field,
    field_on,
    is_code,
    realtime,
    realtime_field,
    to_float,
    to_int,
    truncate_status,
)


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [
        ("38.5", 38.5),
        ("0", 0.0),
        ("0.0", 0.0),
        (7, 7.0),
        (-1000, -1000.0),
        # ausente o ilegible → None, NUNCA 0.0: un campo que no viene no es un cero.
        (None, None),
        ("", None),
        ("None", None),
        ("abc", None),
        ([], None),
    ],
)
def test_to_float(valor, esperado) -> None:
    assert to_float(valor) == esperado


def test_to_int_acepta_los_enteros_que_el_coche_manda_como_decimales() -> None:
    """El coche manda `"1.0"` tan a menudo como `"1"`, y `int("1.0")` lanza ValueError."""
    assert to_int("1.0") == 1
    assert to_int("1") == 1
    assert to_int("2.9") == 2
    assert to_int(None) is None
    assert to_int("nope") is None


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [("1", True), ("1.0", True), (1, True), ("0", False), ("2", False), (None, False)],
)
def test_is_code(valor, esperado) -> None:
    assert is_code(valor, 1) is esperado


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [
        # encendido / abierto
        ("1", True),
        ("3", True),
        (1, True),
        ("true", True),
        # apagado / cerrado — "0.0" incluido: es la alineación entre binary/lock/switch/cover
        ("0", False),
        ("0.0", False),
        ("false", False),
        ("off", False),
        ("no", False),
        # ausente → None, para que emerja el valor restaurado y no un falso False
        (None, None),
        ("", None),
        ("None", None),
        ("   ", None),
    ],
)
def test_field_on(valor, esperado) -> None:
    assert field_on(valor) is esperado


def test_realtime_siempre_devuelve_un_dict() -> None:
    """`data["realtime"]` es None hasta la primera sonda: ese None era el origen de los once
    `or {}` repartidos por sensor/binary_sensor/coordinator."""
    assert realtime(None) == {}
    assert realtime({}) == {}
    assert realtime({"realtime": None}) == {}
    assert realtime({"realtime": {"dumpEnergy": "80"}}) == {"dumpEnergy": "80"}


def test_realtime_field() -> None:
    data = {"realtime": {"odometer": 1234, "vacio": "", "nulo": None}}
    assert realtime_field(data, "odometer") == "1234"
    assert realtime_field(data, "vacio") is None
    assert realtime_field(data, "nulo") is None
    assert realtime_field(data, "inexistente") is None
    assert realtime_field(None, "odometer") is None


def test_truncate_status_respeta_el_limite_de_estado_de_ha() -> None:
    """Pasarse del tope no da error visible: la entidad deja de actualizarse sin más."""
    largo = "x" * (MAX_STATUS_LEN + 50)
    assert len(truncate_status(largo)) == MAX_STATUS_LEN
    assert truncate_status("corto") == "corto"
    assert truncate_status(42) == "42"


# ───────────────────────── `field`: los dos canales ─────────────────────────
# El coche informa de las mismas claves por dos vías: los push 5A02 por MQTT (eventos: el coche
# los emite cuando algo cambia) y la sonda realtime (la última instantánea que guardó la nube).
# Manda el push; la sonda solo rellena huecos.


def test_field_prefiere_el_push_sobre_la_instantanea() -> None:
    """El fallo del 04/09/2026, medido en el historial: el maletero se cerró a las 12:21:50 y el
    push lo reflejó; a las 12:26:51 venció la ventana de «coche despierto» y 2 ms después la
    entidad volvía a «abierto», porque la instantánea de la nube seguía con `trunkDoor=1`.

    Había una regla que invertía la prioridad con el coche dormido. La instantánea puede ser de
    hace horas; el push es de cuando algo cambió."""
    data = {"fields": {"trunkDoor": "0"}, "realtime": {"trunkDoor": "1"}, "awake": False}

    assert field(data, "trunkDoor") == "0"


def test_field_cae_a_la_sonda_cuando_el_push_no_trae_la_clave() -> None:
    """El motivo por el que existe el respaldo: sin MQTT (cuentas invitadas) `fields` está
    vacío y las entidades se quedaban congeladas."""
    assert field({"fields": {}, "realtime": {"doorLock": "1"}}, "doorLock") == "1"
    assert field({"fields": {"otra": "0"}, "realtime": {"doorLock": "1"}}, "doorLock") == "1"


def test_field_sin_datos_es_none() -> None:
    """Arranque en frío: ni push ni sonda. `None` (y no `0`) es lo que deja emerger el estado
    restaurado en vez de un falso «cerrado»."""
    assert field(None, "doorLock") is None
    assert field({}, "doorLock") is None
    assert field({"fields": {}, "realtime": None}, "doorLock") is None
