"""Constantes compartidas por los tests de Ebro Auto.

⚠️ `TEST_VIN` MANDA SOBRE TODOS LOS SNAPSHOTS. `entity.py` deriva el entity_id como
`<plataforma>.ebro_<4 últimas del VIN>_<descriptor>`, así que cambiar el VIN aquí renombra
las ~93 entidades de golpe y obliga a regenerar todos los `.ambr`. Elegido una vez, no se toca.
"""

from custom_components.ebro.const import (
    CONF_AREA_CODE,
    CONF_BFF,
    CONF_CAR_MQTT_HOST,
    CONF_CAR_MQTT_PORT,
    CONF_CHANNEL_ID,
    CONF_PASSWORD,
    CONF_PHONE,
    CONF_PIN,
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
    DATA_VEHICLE_BRAND,
    DATA_VEHICLE_MODEL,
)

TEST_VIN = "LSJA0000000000001"
"""VIN ficticio. Las 4 últimas cifras (`0001`) prefijan todos los entity_id: `ebro_0001_*`."""

TEST_VIN4 = TEST_VIN[-4:]
TEST_TUSERID = "tuser-0000000000"
TEST_PHONE = "600000000"
TEST_PASSWORD = "contrasena-de-prueba"
TEST_PIN = "1234"

#: Instante congelado en todos los tests con snapshot. Invierno → sin horario de verano,
#: relevante para `_local_min_to_utc_min` del coordinator.
FROZEN_TIME = "2026-01-15 12:00:00+00:00"

ENTRY_DATA = {
    CONF_PHONE: TEST_PHONE,
    CONF_PASSWORD: TEST_PASSWORD,
    CONF_PIN: TEST_PIN,
    CONF_AREA_CODE: "34",
    CONF_VIN: TEST_VIN,
    CONF_TUSERID: TEST_TUSERID,
    CONF_SIGN_KEY: "",
    CONF_BFF: "https://legend.example.invalid/api",
    CONF_TSP_HOST: "https://tspconsole.example.invalid",
    CONF_CAR_MQTT_HOST: "mqtt.example.invalid",
    CONF_CAR_MQTT_PORT: 8083,
    CONF_CHANNEL_ID: "4",
    CONF_VEHICLE_NAME: "Ebro S900",
    DATA_VEHICLE_MODEL: "S900",
    DATA_VEHICLE_BRAND: "Ebro",
}

ENTRY_OPTIONS = {
    CONF_POLL_PARKED: 0,
    CONF_POLL_CHARGING: 15,
    CONF_POLL_PLUGGED: 30,
    CONF_POLL_MOVING: 3,
    CONF_POLL_MOVING_IDLE: 5,
}
