"""Tests de `vehicle/mqtt_client.py` — el ciclo de vida del cliente paho.

Lo que se prueba aquí es el ESTADO que el cliente comunica hacia arriba, no paho. Importa
porque el caso que motivó estos tests —conexión aceptada, suscripción denegada— es invisible
desde fuera: el coordinator veía `car_connected: true` y ni un solo mensaje, exactamente igual
que si el coche estuviera dormido.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from custom_components.ebro.vehicle.mqtt_client import EbroMqttClient, MqttConfig

CONFIG = MqttConfig(
    host="broker.example", port=8083, tuserid="U123", channel_id="4", certs_dir="/certs"
)


@pytest.fixture
def cliente_paho():
    """Un doble de `paho.mqtt.Client` que guarda los callbacks que le asignan."""
    fake = MagicMock()
    with (
        patch("paho.mqtt.client.Client", return_value=fake),
        patch(
            "custom_components.ebro.vehicle.certificates.tls_paths",
            return_value={"ca_certs": "ca", "certfile": "c", "keyfile": "k"},
        ),
    ):
        yield fake


def _conectar(cliente_paho, **callbacks):
    defaults = {
        "on_message": lambda payload: None,
        "on_connected": lambda ok, rc: None,
        "on_disconnected": lambda rc: None,
    }
    client = EbroMqttClient(CONFIG, **{**defaults, **callbacks})
    client.connect()
    return client


def test_identidad_que_exige_la_acl_del_broker() -> None:
    """Client id y topic van en la ACL del broker: cambiar el formato es que rechace."""
    assert CONFIG.client_id == "app_4_U123"
    assert CONFIG.topic == "app/4/U123/account/msgCenter/msg"


def test_la_conexion_suscribe_el_topic(cliente_paho) -> None:
    _conectar(cliente_paho)

    cliente_paho.on_connect(cliente_paho, None, None, 0)

    cliente_paho.subscribe.assert_called_once_with(CONFIG.topic, qos=1)


def test_una_suscripcion_concedida_se_publica_como_ok(cliente_paho) -> None:
    resultados: list[tuple] = []
    _conectar(cliente_paho, on_subscribed=lambda ok, detail: resultados.append((ok, detail)))

    cliente_paho.on_subscribe(cliente_paho, None, 1, [1])

    assert resultados == [(True, "1")]


def test_una_suscripcion_denegada_se_distingue_de_estar_conectado(cliente_paho) -> None:
    """0x80 = el broker acepta la conexión y DENIEGA el topic. Sin este aviso la integración
    se queda conectada y muda para siempre, sin nada que lo delate."""
    resultados: list[tuple] = []
    _conectar(cliente_paho, on_subscribed=lambda ok, detail: resultados.append((ok, detail)))

    cliente_paho.on_subscribe(cliente_paho, None, 1, [0x80])

    assert resultados == [(False, "128")]


def test_un_suback_vacio_no_se_da_por_bueno(cliente_paho) -> None:
    """Sin códigos no hay concesión que dar por buena: es un «no sé», y `all()` sobre una lista
    vacía habría dicho que sí."""
    resultados: list[tuple] = []
    _conectar(cliente_paho, on_subscribed=lambda ok, detail: resultados.append((ok, detail)))

    cliente_paho.on_subscribe(cliente_paho, None, 1, [])

    assert resultados == [(False, "sin respuesta")]


def test_una_conexion_rechazada_no_suscribe(cliente_paho) -> None:
    conexiones: list[tuple] = []
    _conectar(cliente_paho, on_connected=lambda ok, rc: conexiones.append((ok, rc)))

    cliente_paho.on_connect(cliente_paho, None, None, 5)   # 5 = no autorizado

    assert conexiones == [(False, 5)]
    cliente_paho.subscribe.assert_not_called()
