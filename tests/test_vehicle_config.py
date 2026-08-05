"""Tests de `vehicle_config.py` — el entry parseado y la única fábrica del `CoreCtx`.

Había dos fábricas del mismo objeto (una en el config flow, otra en el coordinator) que
rellenaban los mismos doce campos. El riesgo no era teórico: añadir un parámetro de región y
olvidarse de una de las dos deja el alta funcionando y el runtime roto, o al revés, con un
fallo que solo se ve en producción. Estos tests fijan que las dos rutas producen lo mismo.
"""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ebro.const import (
    CONF_BFF,
    CONF_CHANNEL_ID,
    CONF_PHONE,
    CONF_PIN,
    CONF_TSP_HOST,
    CONF_TUSERID,
    CONF_VIN,
    DEFAULTS,
)
from custom_components.ebro.core import tsp_sign
from custom_components.ebro.vehicle.config import CORE_DIR, VehicleConfig, build_ctx

from .const import ENTRY_DATA, TEST_VIN

#: Los campos que ambas rutas deben rellenar igual cuando parten de los mismos datos.
_CAMPOS_COMPARTIDOS = ("vin", "tuserid", "pin", "email", "sign_key", "bff", "tsp_host",
                       "channel_id")


def test_las_dos_fabricas_coinciden() -> None:
    """La razón de ser del módulo: mismo dato de entrada, mismo `CoreCtx`."""
    datos = {
        CONF_VIN: TEST_VIN,
        CONF_TUSERID: "12345",
        CONF_PIN: "1234",
        CONF_PHONE: "600000000",
        CONF_BFF: DEFAULTS[CONF_BFF],
        CONF_TSP_HOST: DEFAULTS[CONF_TSP_HOST],
        CONF_CHANNEL_ID: DEFAULTS[CONF_CHANNEL_ID],
    }
    entry = MockConfigEntry(domain="ebro", data=datos)

    desde_entry = VehicleConfig.from_entry(entry)
    desde_flow = VehicleConfig.from_flow_data(datos)

    for campo in _CAMPOS_COMPARTIDOS:
        assert getattr(desde_entry, campo) == getattr(desde_flow, campo), campo


def test_from_entry_aplica_los_valores_de_region_por_defecto() -> None:
    """Una entrada mínima (sin los ajustes avanzados) tiene que quedar apuntando a Europa."""
    entry = MockConfigEntry(domain="ebro", data={CONF_VIN: TEST_VIN, CONF_TUSERID: "1"})

    config = VehicleConfig.from_entry(entry)

    assert config.bff == DEFAULTS[CONF_BFF]
    assert config.tsp_host == DEFAULTS[CONF_TSP_HOST]
    assert config.car_port == DEFAULTS["car_mqtt_port"]


def test_la_firma_cae_en_la_constante_de_la_app() -> None:
    """El config flow ya no pide la HALF: solo se respeta si una entrada vieja la trae."""
    nueva = VehicleConfig.from_entry(MockConfigEntry(domain="ebro", data=ENTRY_DATA))
    vieja = VehicleConfig.from_entry(
        MockConfigEntry(domain="ebro", data={**ENTRY_DATA, "sign_key": "GUARDADA"})
    )

    assert nueva.sign_key == tsp_sign.HALF
    assert vieja.sign_key == "GUARDADA"


def test_el_telefono_sustituye_al_email_en_las_entradas_nuevas() -> None:
    """El teléfono pasó a ser el identificador de acceso (2026-07-27); las entradas viejas
    siguen guardando `email` y tienen que seguir funcionando."""
    nueva = VehicleConfig.from_entry(
        MockConfigEntry(domain="ebro", data={CONF_VIN: "V", CONF_TUSERID: "1",
                                            CONF_PHONE: "600000000"})
    )
    vieja = VehicleConfig.from_entry(
        MockConfigEntry(domain="ebro", data={CONF_VIN: "V", CONF_TUSERID: "1",
                                            "email": "yo@example.com"})
    )

    assert nueva.email == "600000000"
    assert vieja.email == "yo@example.com"


def test_build_ctx_transporta_la_config_y_las_rutas(tmp_path) -> None:
    config = VehicleConfig(vin=TEST_VIN, tuserid="1", pin="1234", channel_id="4")
    token = str(tmp_path / "token.json")
    taskid = str(tmp_path / "taskid.txt")

    ctx = build_ctx(config, token_path=token, taskid_file=taskid)

    assert (ctx.vin, ctx.tuserid, ctx.pin, ctx.channel_id) == (TEST_VIN, "1", "1234", "4")
    assert (ctx.token_path, ctx.taskid_file) == (token, taskid)
    assert ctx.src_dir == CORE_DIR


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [("1", True), ("si", True), (None, True), ("0", False), ("", False), ("false", False),
     ("no", False)],
)
def test_mint_taskid_se_lee_del_entorno_una_sola_vez(monkeypatch, valor, esperado) -> None:
    """Sin generación de taskId los comandos no pueden partir, así que solo se desactiva para
    diagnóstico. La lectura estaba enterrada dentro del coordinator; ahora tiene un sitio."""
    if valor is None:
        monkeypatch.delenv("EBRO_MINT_TASKID", raising=False)
    else:
        monkeypatch.setenv("EBRO_MINT_TASKID", valor)

    entry = MockConfigEntry(domain="ebro", data={CONF_VIN: "V", CONF_TUSERID: "1"})

    assert VehicleConfig.from_entry(entry).mint_taskid is esperado
