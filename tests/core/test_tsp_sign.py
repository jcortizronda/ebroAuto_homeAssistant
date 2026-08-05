"""Tests de `core/tsp_sign.py` — la firma REST del SDK Chery.

`ts_ms` es siempre un argumento explícito, así que el módulo es 100 % reproducible sin
congelar el reloj: se firma contra vectores dorados con un timestamp fijo. Si algún día la
firma deja de reproducir estos valores, todas las peticiones al TSP dejarían de aceptarse —
es exactamente el fallo que este archivo debe detectar antes de llegar al coche.
"""

from __future__ import annotations

import pytest
from syrupy.assertion import SnapshotAssertion

from custom_components.ebro.core import tsp_sign

TS = 1700000000000
"""Timestamp fijo de los vectores dorados."""


def test_half_secret_conocida() -> None:
    """La HALF documentada, verificada contra peticiones reales capturadas."""
    assert tsp_sign.HALF == "EUEBROProd89ec59274d23491084af"
    assert tsp_sign.half_secret("abcdef") == "ace"


def test_vectores_dorados(snapshot: SnapshotAssertion) -> None:
    """Firma de un body plano y de uno con array anidado, con timestamp fijo."""
    plano = tsp_sign.build_sign({"vin": "LSJA0000000000001"}, TS)
    anidado = tsp_sign.build_sign(
        {
            "vin": "LSJA0000000000001",
            "mainSwitch": 1,
            "chargeAppointPlans": [
                {"switchStatus": 1, "startTime": 465, "duration": 360},
            ],
        },
        TS,
    )
    escalares = tsp_sign.build_sign({"vin": "V", "cycleData": [1, 2, 3, 4, 5, 6, 7]}, TS)

    assert {"plano": plano, "anidado": anidado, "escalares": escalares} == snapshot


def test_build_sign_es_base64_mayuscula() -> None:
    """base64(sha256(base)).upper(), NO hexdigest — el descubrimiento de la captura S23."""
    sign = tsp_sign.build_sign({"vin": "V"}, TS)
    assert sign == sign.upper()
    assert sign.endswith("=")
    assert len(sign) == 44  # 32 bytes en base64


def test_build_sign_ignora_el_orden_de_las_claves() -> None:
    """`Arrays.sort` sobre las claves: el orden de inserción del dict no debe influir."""
    a = tsp_sign.build_sign({"vin": "V", "pin": "1234"}, TS)
    b = tsp_sign.build_sign({"pin": "1234", "vin": "V"}, TS)
    assert a == b


@pytest.mark.parametrize("vacio", [None, ""])
def test_build_sign_salta_los_valores_vacios(vacio) -> None:
    """`null` y `""` se saltan → firmar con ellos equivale a no incluirlos."""
    con = tsp_sign.build_sign({"vin": "V", "extra": vacio}, TS)
    sin = tsp_sign.build_sign({"vin": "V"}, TS)
    assert con == sin


def test_build_sign_depende_del_timestamp() -> None:
    assert tsp_sign.build_sign({"vin": "V"}, TS) != tsp_sign.build_sign({"vin": "V"}, TS + 1)


def test_flatten_escalares_sin_separador() -> None:
    """`[1,2,3]` → `'123'`, verificado byte a byte contra envelopes reales."""
    assert tsp_sign._flatten_value([1, 2, 3, 4, 5, 6, 7]) == "1234567"


def test_flatten_objetos_ordena_y_salta_vacios() -> None:
    plano = tsp_sign._flatten_value([{"b": 2, "a": 1, "c": "", "d": None}])
    assert plano == "a=1&b=2"


def test_flatten_obj_es_noop_para_bodies_planos() -> None:
    """Sin arrays el algoritmo histórico queda idéntico (63/63 envelopes planos)."""
    plano = {"vin": "V", "pin": "1234", "n": 7}
    assert tsp_sign._flatten_obj(plano) == plano


def test_sign_body_conserva_el_array_real() -> None:
    """`build_sign` aplana una COPIA: el body enviado debe llevar la lista original, no la
    cadena aplanada — si no, el backend rechazaría la petición."""
    plans = [{"switchStatus": 1, "startTime": 465}]
    body = tsp_sign.sign_body({"vin": "V", "chargeAppointPlans": plans}, TS)

    assert body["chargeAppointPlans"] == plans
    assert body["appId"] == tsp_sign.APP_ID
    assert "sign" in body


def test_sign_body_firma_incluyendo_el_appid_pero_no_el_sign() -> None:
    """`appId` entra en los parámetros firmados; `sign` se añade DESPUÉS."""
    body = tsp_sign.sign_body({"vin": "V"}, TS)
    esperado = tsp_sign.build_sign({"vin": "V", "appId": tsp_sign.APP_ID}, TS)
    assert body["sign"] == esperado


def test_sign_body_acepta_una_half_alternativa() -> None:
    otra = tsp_sign.sign_body({"vin": "V"}, TS, half="OTRA")
    assert otra["sign"] != tsp_sign.sign_body({"vin": "V"}, TS)["sign"]


def test_auth_headers() -> None:
    assert tsp_sign.auth_headers("tok", TS) == {
        "Authorization": "tok",
        "timestamp": str(TS),
        "x-TenantId": "",
    }
    assert tsp_sign.auth_headers("tok", TS, tenant_id="euebro")["x-TenantId"] == "euebro"


def test_app_id_se_lee_en_tiempo_de_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """`APP_ID` se resuelve del entorno al importar el módulo.

    Consecuencia práctica para quien escriba tests: `monkeypatch.setenv("TSP_APP_ID", …)` no
    hace nada; hay que parchear el atributo del módulo.
    """
    monkeypatch.setattr(tsp_sign, "APP_ID", "otro-app-id")
    assert tsp_sign.sign_body({"vin": "V"}, TS)["appId"] == "otro-app-id"
