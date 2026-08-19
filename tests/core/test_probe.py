"""Tests de `core/probe.py` — la sonda de solo lectura del canal realtime.

Se mockean `probe.W._bff_login` y `probe.W._signed_post` (`W` es el alias de `wake` dentro
del módulo): es la frontera que el propio autotest del módulo ya trata como costura.
El log en crudo de la sonda queda vacío por defecto en el `CoreCtx`, así que no se escribe ningún archivo.
"""

from __future__ import annotations

import time
from unittest.mock import Mock, patch

import pytest
from syrupy.assertion import SnapshotAssertion

from custom_components.ebro.core import probe
from custom_components.ebro.core.context import CoreCtx

REALTIME_OK = {
    "code": "000000",
    "body": {
        "lat": "40.416775",
        "lon": "-3.703790",
        "altitude": "667",
        "direction": "180",
        "dumpEnergy": "64.5",
        "vehicleSpeed": "0",
        "odometer": "12345",
        "pureElectricRange": "60",
        "chargeState": "1",
        "onlineStatus": "1",
    },
}
DORMIDO = {"code": "A07900"}


@pytest.fixture
def ctx() -> CoreCtx:
    return CoreCtx(vin="LSJA0000000000001")


def _post(por_path: dict):
    """Fabrica un `_signed_post` que responde según el path pedido."""

    def _fake(_ctx, _ut, path, _params):
        for fragmento, respuesta in por_path.items():
            if fragmento in path:
                return 200, respuesta
        return 200, DORMIDO

    return _fake


# ───────────────────────── _rich (puro) ─────────────────────────


def test_rich_excluye_la_posicion(snapshot: SnapshotAssertion) -> None:
    """`probe_status` acaba en el estado de un sensor (y por tanto en la base de datos de HA),
    en «Descargar diagnóstico» y en el log. Escribir ahí lat/lon dejaba en claro dónde está el
    coche en los tres sitios — encontrado en campo el 2026-07-20.
    """
    rich = probe._rich(REALTIME_OK["body"])

    assert not (set(rich) & set(probe._GEO_KEYS))
    assert rich == snapshot


def test_rich_conserva_lo_util_para_diagnostico() -> None:
    rich = probe._rich(REALTIME_OK["body"])
    assert rich["odometer"] == "12345"
    assert rich["dumpEnergy"] == "64.5"
    assert rich["onlineStatus"] == "1"


@pytest.mark.parametrize("data", [{}, {"campo": "sin interés"}, None, "texto"])
def test_rich_tolera_entradas_raras(data) -> None:
    assert probe._rich(data) == {}


def test_geo_keys_son_subconjunto_de_rich_keys() -> None:
    assert set(probe._GEO_KEYS) <= set(probe.RICH_KEYS)
    assert set(probe._MSG_KEYS) == set(probe.RICH_KEYS) - set(probe._GEO_KEYS)


# ───────────────────────── probe_once ─────────────────────────


def test_probe_once_con_datos_en_vivo(ctx: CoreCtx) -> None:
    publicados: list[str] = []
    on_data = Mock()

    with (
        patch.object(probe.W, "_bff_login", return_value=("UT", "TU")),
        patch.object(probe.W, "_signed_post", _post({"realtime": REALTIME_OK})),
    ):
        res = probe.probe_once(ctx, publicados.append, force=True, on_data=on_data)

    assert res["ok"] is True
    assert res["online"] is True
    assert res["got_data"] is True

    # `on_data` recibe el dict EN CRUDO: la posición viaja intacta al device_tracker aunque
    # esté excluida del mensaje legible.
    on_data.assert_called_once()
    crudo = on_data.call_args[0][0]
    assert crudo["lat"] == "40.416775"

    # ...y NO aparece en ningún texto publicado
    assert not any("40.416775" in p for p in publicados)


def test_probe_once_coche_dormido(ctx: CoreCtx) -> None:
    publicados: list[str] = []
    on_data = Mock()

    with (
        patch.object(probe.W, "_bff_login", return_value=("UT", "TU")),
        patch.object(probe.W, "_signed_post", _post({})),
    ):
        res = probe.probe_once(ctx, publicados.append, force=True, on_data=on_data)

    assert res["ok"] is True
    assert res["online"] is False
    assert res["got_data"] is False
    assert res["codes"] == ["A07900", "A07900", "A07900"]
    on_data.assert_not_called()


def test_probe_once_sesion_caducada(ctx: CoreCtx) -> None:
    publicados: list[str] = []

    with patch.object(probe.W, "_bff_login", return_value=(None, None)):
        res = probe.probe_once(ctx, publicados.append, force=True)

    assert res == {"ok": False, "reason": "no_usertoken"}
    assert any("sesión caducada" in p.lower() for p in publicados)


def test_probe_once_respeta_el_cooldown(ctx: CoreCtx) -> None:
    """El cooldown es POR VEHÍCULO (vive en el contexto): con dos coches, la sonda de uno ya
    no consume el del otro."""
    ctx.state.last_probe_ts = time.time()

    with patch.object(probe.W, "_bff_login") as login:
        res = probe.probe_once(ctx, lambda _m: None)

    assert res["reason"] == "cooldown"
    assert res["wait_s"] <= ctx.probe_cooldown_s
    login.assert_not_called()


def test_el_cooldown_se_anuncia_en_vez_de_volver_en_silencio(ctx: CoreCtx) -> None:
    """Un retorno mudo era indistinguible de una integración rota: quien pulsaba «Actualizar
    ubicación» no veía ni un mensaje ni un cambio de estado."""
    ctx.state.last_probe_ts = time.time()
    publicados: list[str] = []

    with patch.object(probe.W, "_bff_login"):
        probe.probe_once(ctx, publicados.append)

    assert any("espero" in m and "s" in m for m in publicados)


def test_probe_once_force_ignora_el_cooldown(ctx: CoreCtx) -> None:
    ctx.state.last_probe_ts = time.time()

    with (
        patch.object(probe.W, "_bff_login", return_value=("UT", "TU")) as login,
        patch.object(probe.W, "_signed_post", _post({"realtime": REALTIME_OK})),
    ):
        probe.probe_once(ctx, lambda _m: None, force=True)

    login.assert_called_once()


def test_probe_once_uno_cada_vez(ctx: CoreCtx) -> None:
    ctx.state.probe_lock.acquire()
    try:
        res = probe.probe_once(ctx, lambda _m: None, force=True)
    finally:
        ctx.state.probe_lock.release()

    assert res == {"ok": False, "reason": "busy"}


def test_probe_once_nunca_lanza(ctx: CoreCtx) -> None:
    publicados: list[str] = []

    with patch.object(probe.W, "_bff_login", side_effect=RuntimeError("boom")):
        res = probe.probe_once(ctx, publicados.append, force=True)

    assert res["ok"] is False
    assert res["reason"] == "exception"
    assert any("Error de sonda" in p for p in publicados)
    # y el lock queda liberado
    assert ctx.state.probe_lock.acquire(blocking=False) is True
    ctx.state.probe_lock.release()


def test_probe_once_un_on_data_roto_no_tumba_la_sonda(ctx: CoreCtx) -> None:
    publicados: list[str] = []

    with (
        patch.object(probe.W, "_bff_login", return_value=("UT", "TU")),
        patch.object(probe.W, "_signed_post", _post({"realtime": REALTIME_OK})),
    ):
        res = probe.probe_once(
            ctx, publicados.append, force=True, on_data=Mock(side_effect=ValueError("mal"))
        )

    assert res["ok"] is True
    assert any("error al publicar datos" in p.lower() for p in publicados)


def test_probe_once_combina_los_tres_endpoints(ctx: CoreCtx) -> None:
    """Realtime tiene prioridad; location y travel añaden campos extra."""
    with (
        patch.object(probe.W, "_bff_login", return_value=("UT", "TU")),
        patch.object(
            probe.W,
            "_signed_post",
            _post(
                {
                    "realtime": REALTIME_OK,
                    "queryVehicleLocation": {"code": "000000", "data": {"gpsTime": "123"}},
                    "travelQuery": {"code": "000000", "data": {"totalKm": "999"}},
                }
            ),
        ),
    ):
        on_data = Mock()
        probe.probe_once(ctx, lambda _m: None, force=True, on_data=on_data)

    crudo = on_data.call_args[0][0]
    assert crudo["gpsTime"] == "123"  # de location
    assert crudo["totalKm"] == "999"  # de travel
    assert crudo["dumpEnergy"] == "64.5"  # de realtime (prioritario)


# ───────────────────── de cuándo son los datos de la sonda ─────────────────────


def test_freshness_con_el_coche_despierto() -> None:
    """`onlineStatus=1`: ha contestado el coche, el dato es de ahora mismo."""
    assert "tiempo real" in probe.freshness({"onlineStatus": "1", "time": "1000"}, now=99999)


def test_freshness_con_el_coche_dormido_dice_la_edad() -> None:
    """El caso que engañaba: el endpoint responde igual de bien, pero devuelve la última
    instantánea que guardó la nube. Anunciarla como «tiempo real con el coche despierto»
    mandaba a buscar el fallo donde no estaba."""
    ahora = 1_787_092_000.0
    hace_26_min = str(int((ahora - 26 * 60) * 1000))

    mensaje = probe.freshness({"onlineStatus": "0", "time": hace_26_min}, now=ahora)

    assert "26 min" in mensaje
    assert "dormido" in mensaje
    assert "tiempo real" not in mensaje


def test_freshness_sin_marca_de_tiempo_no_se_inventa_una() -> None:
    mensaje = probe.freshness({"onlineStatus": "0"}, now=1_787_092_000.0)

    assert "dormido" in mensaje
    assert "min" not in mensaje


def test_freshness_cae_a_result_time_si_no_hay_time() -> None:
    """`time` es la marca buena, pero no siempre viene."""
    ahora = 1_787_092_000.0
    hace_5_min = str(int((ahora - 5 * 60) * 1000))

    assert "5 min" in probe.freshness({"onlineStatus": "0", "resultTime": hace_5_min}, now=ahora)
