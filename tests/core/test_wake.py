"""Tests de `core/wake.py` — token, login BFF y orquestación del despertar.

Estrategia: los parsers de respuesta son puros y se prueban directos; para el resto se
mockea la costura de más alto nivel posible (`ebro_login.refresh_token` en vez de
`requests`), que es la que el propio módulo trata como frontera.
"""

from __future__ import annotations

import json
import os
import time
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
import pytest

from custom_components.ebro.core import wake
from custom_components.ebro.core.context import CoreCtx


@pytest.fixture
def ctx(tmp_path) -> CoreCtx:
    """Contexto con las rutas dentro de `tmp_path`.

    Sin esto se escribiría en `core/token.json`, DENTRO del paquete instalado.
    """
    return CoreCtx(
        vin="LSJA0000000000001",
        token_path=str(tmp_path / "token.json"),
        taskid_file=str(tmp_path / "taskid.txt"),
    )


def _escribe_token(ctx: CoreCtx, **campos) -> None:
    payload = {"access_token": "AT", "refresh_token": "RT", "expires_in": 43200, **campos}
    with open(ctx.token_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)


# ───────────────────────── parsers puros ─────────────────────────


@pytest.mark.parametrize(
    ("j", "esperado"),
    [
        ({"code": "000000"}, "000000"),
        ({}, None),
        ("no es un dict", "no es un dict"),
        (None, None),
    ],
)
def test_code_of(j, esperado) -> None:
    assert wake._code_of(j) == esperado


def test_payload_prefiere_data_sobre_body() -> None:
    assert wake._payload({"data": {"a": 1}, "body": {"b": 2}}) == {"a": 1}


def test_payload_cae_en_body() -> None:
    """`/asr/manager/realtime` responde bajo `body`: sin esta rama se perdían los 84 campos."""
    assert wake._payload({"body": {"dumpEnergy": "80"}}) == {"dumpEnergy": "80"}


@pytest.mark.parametrize(
    "j",
    [
        {},
        {"data": {}},  # dict vacío = sin payload útil
        {"data": None},
        {"data": "texto"},  # no-dict
        "no es un dict",
        None,
    ],
)
def test_payload_sin_datos(j) -> None:
    assert wake._payload(j) is None
    assert wake._has_live_data(j) is False


def test_has_live_data() -> None:
    assert wake._has_live_data({"body": {"x": 1}}) is True


def test_huella_es_sha256_estable() -> None:
    """Se compara la huella, nunca la credencial en claro."""
    h = wake._fingerprint("RT")
    assert h == wake._fingerprint("RT")
    assert h != wake._fingerprint("OTRO")
    assert len(h) == 64
    assert "RT" not in h


# ───────────────────────── _access_token ─────────────────────────


def test_access_token_desde_la_raiz(ctx: CoreCtx) -> None:
    _escribe_token(ctx)
    assert wake._access_token(ctx) == "AT"


def test_access_token_desde_data(ctx: CoreCtx) -> None:
    with open(ctx.token_path, "w", encoding="utf-8") as fh:
        json.dump({"data": {"access_token": "AT-anidado"}}, fh)

    assert wake._access_token(ctx) == "AT-anidado"


def test_access_token_json_no_dict(ctx: CoreCtx) -> None:
    with open(ctx.token_path, "w", encoding="utf-8") as fh:
        json.dump(["no", "es", "un", "dict"], fh)

    assert wake._access_token(ctx) is None


# ───────────────────────── _eta_token ─────────────────────────


def test_eta_token_sin_archivo(ctx: CoreCtx) -> None:
    assert wake._eta_token(ctx) == (-1.0, 0)


def test_eta_token_edad_sobre_el_mtime(ctx: CoreCtx, freezer: FrozenDateTimeFactory) -> None:
    """El token es opaco (no es un JWT): la edad se mide sobre el mtime del archivo, que se
    reescribe en cada renovación."""
    _escribe_token(ctx, expires_in=43200)
    freezer.tick(3600)

    eta, lifetime = wake._eta_token(ctx)

    assert lifetime == 43200
    assert eta == pytest.approx(3600, abs=5)


def test_eta_token_expires_in_ilegible(ctx: CoreCtx) -> None:
    _escribe_token(ctx, expires_in="no-un-numero")
    assert wake._eta_token(ctx)[1] == 0


# ───────────────────────── _refresh_token_detail ─────────────────────────


def test_refresh_sin_archivo_es_ausente(ctx: CoreCtx) -> None:
    ok, reason = wake._refresh_token_detail(ctx)

    assert (ok, reason) == (False, "ausente")
    assert ctx.state.refresh_reason == "ausente"


def test_refresh_sin_refresh_token_es_ausente(ctx: CoreCtx) -> None:
    with open(ctx.token_path, "w", encoding="utf-8") as fh:
        json.dump({"access_token": "AT"}, fh)

    assert wake._refresh_token_detail(ctx) == (False, "ausente")


def test_refresh_ok_reescribe_el_token_de_forma_atomica(ctx: CoreCtx) -> None:
    _escribe_token(ctx)
    nuevo = {"access_token": "AT2", "refresh_token": "RT2", "expires_in": 43200}

    with patch(
        "custom_components.ebro.core.ebro_login.refresh_token",
        return_value=(True, {"raw": nuevo}),
    ):
        ok, reason = wake._refresh_token_detail(ctx)

    assert (ok, reason) == (True, "")
    with open(ctx.token_path, encoding="utf-8") as fh:
        assert json.load(fh) == nuevo
    # el temporal de la escritura atómica no debe quedar por ahí
    assert not os.path.exists(ctx.token_path + ".tmp")
    # el token es una credencial: legible solo por el propietario
    assert oct(os.stat(ctx.token_path).st_mode)[-3:] == "600"


def test_refresh_error_de_red_no_es_una_revocacion(ctx: CoreCtx) -> None:
    """La distinción es el objetivo de toda la función: sin ella una conexión inestable haría
    aparecer «Reautenticar» y el usuario reautenticaría en balde."""
    _escribe_token(ctx)

    with patch(
        "custom_components.ebro.core.ebro_login.refresh_token",
        side_effect=OSError("timeout"),
    ):
        ok, reason = wake._refresh_token_detail(ctx)

    assert ok is False
    assert reason == "red:OSError"
    assert ctx.state.refresh_burned == ""  # el freno NO se arma por un fallo de red


def test_refresh_rechazado_arma_el_freno(ctx: CoreCtx) -> None:
    """Un refresh_token revocado no vuelve a ser válido solo: reintentarlo en cada llamada es
    solo ruido hacia el gateway (medido: 5 intentos idénticos en 6 minutos)."""
    _escribe_token(ctx)

    with patch(
        "custom_components.ebro.core.ebro_login.refresh_token",
        return_value=(False, "invalid_grant"),
    ) as refresh:
        ok, reason = wake._refresh_token_detail(ctx)

        assert ok is False
        assert reason.startswith("rechazado:")
        assert ctx.state.refresh_burned == wake._fingerprint("RT")

        # segundo intento con el MISMO token: ni siquiera se llama al servidor
        refresh.reset_mock()
        ok2, motivo2 = wake._refresh_token_detail(ctx)

    assert (ok2, motivo2) == (False, "rechazado:ya_rechazado")
    refresh.assert_not_called()


def test_el_freno_se_suelta_con_un_refresh_token_distinto(ctx: CoreCtx) -> None:
    """El freno se libera solo en cuanto el archivo contiene otra credencial."""
    _escribe_token(ctx)
    ctx.state.refresh_burned = wake._fingerprint("RT")
    ctx.state.refresh_burned_ts = 1e12  # recentísimo

    _escribe_token(ctx, refresh_token="RT-NUEVO")

    with patch(
        "custom_components.ebro.core.ebro_login.refresh_token",
        return_value=(True, {"raw": {"access_token": "AT3"}}),
    ) as refresh:
        ok, _ = wake._refresh_token_detail(ctx)

    assert ok is True
    refresh.assert_called_once()


def test_el_freno_expira_tras_la_ventana_de_reintento(
    ctx: CoreCtx, freezer: FrozenDateTimeFactory
) -> None:
    """Se reintenta una vez cada `REINTENTA_REFRESH_TRAS_S` por si el rechazo fue un desliz
    del servidor: quedarse bloqueado para siempre sería peor."""
    _escribe_token(ctx)
    ctx.state.refresh_burned = wake._fingerprint("RT")
    ctx.state.refresh_burned_ts = __import__("time").time()

    freezer.tick(ctx.retry_refresh_after_s + 1)

    with patch(
        "custom_components.ebro.core.ebro_login.refresh_token",
        return_value=(True, {"raw": {"access_token": "AT4"}}),
    ) as refresh:
        wake._refresh_token_detail(ctx)

    refresh.assert_called_once()


def test_doble_comprobacion_detecta_que_otro_hilo_ya_renovo(ctx: CoreCtx) -> None:
    """Rehacer el refresh quemaría el token nuevo e invalidaría la sesión entera."""
    _escribe_token(ctx)

    real_open = open
    llamadas = {"n": 0}

    def _open_que_cambia(path, *args, **kwargs):
        # la 2ª lectura (la de dentro del lock) ve ya el token renovado por otro hilo
        if path == ctx.token_path and "r" in (args[0] if args else kwargs.get("mode", "r")):
            llamadas["n"] += 1
            if llamadas["n"] == 2:
                with real_open(path, "w", encoding="utf-8") as fh:
                    json.dump({"access_token": "AT-OTRO-HILO", "refresh_token": "RT"}, fh)
        return real_open(path, *args, **kwargs)

    with (
        patch("builtins.open", _open_que_cambia),
        patch("custom_components.ebro.core.ebro_login.refresh_token") as refresh,
    ):
        ok, reason = wake._refresh_token_detail(ctx)

    assert (ok, reason) == (True, "")
    refresh.assert_not_called()


def test_refresh_token_compat_devuelve_bool(ctx: CoreCtx) -> None:
    assert wake._refresh_token(ctx) is False


# ───────────────────────── do_wake ─────────────────────────


def test_do_wake_nunca_lanza(ctx: CoreCtx) -> None:
    """Cada error se convierte en un status legible; el botón nunca debe explotar."""
    publicados: list[str] = []

    with patch.object(wake, "_do_wake_inner", side_effect=RuntimeError("boom")):
        res = wake.do_wake(ctx, publicados.append)

    assert res["ok"] is False
    assert res["reason"] == "exception"
    assert any("Error al despertar" in p for p in publicados)


def test_do_wake_es_uno_cada_vez_por_vehiculo(ctx: CoreCtx) -> None:
    """Dos coches pueden despertarse en paralelo; el mismo no."""
    publicados: list[str] = []
    ctx.state.wake_lock.acquire()
    try:
        res = wake.do_wake(ctx, publicados.append)
    finally:
        ctx.state.wake_lock.release()

    assert res == {"ok": False, "reason": "busy"}
    assert any("ya en curso" in p for p in publicados)


def test_do_wake_libera_el_lock_aunque_falle(ctx: CoreCtx) -> None:
    with patch.object(wake, "_do_wake_inner", side_effect=RuntimeError("boom")):
        wake.do_wake(ctx, lambda _m: None)

    assert ctx.state.wake_lock.acquire(blocking=False) is True
    ctx.state.wake_lock.release()


def test_los_ritmos_se_configuran_por_contexto(ctx: CoreCtx) -> None:
    """El cooldown y el número de ciclos son del VEHÍCULO, no del proceso.

    Antes eran constantes de módulo leídas de `os.environ` al importar, y este test se limitaba
    a documentar esa limitación (`monkeypatch.setenv` no servía). Ahora se puede comprobar el
    comportamiento: bajar el cooldown a cero hace que el despertar no lo respete."""
    ctx.state.last_sms_ts = time.time()   # se acaba de mandar un SMS

    ctx_con_cooldown = replace_cooldown(ctx, 300)
    res = wake.do_wake(ctx_con_cooldown, lambda _m: None, is_awake=lambda: False)
    assert res["reason"] == "cooldown"

    ctx_sin_cooldown = replace_cooldown(ctx, 0)
    with patch.object(wake, "_bff_login", return_value=(None, None)):
        res = wake.do_wake(ctx_sin_cooldown, lambda _m: None, is_awake=lambda: False)
    assert res["reason"] == "no_usertoken"   # pasó del cooldown y llegó al login


def replace_cooldown(ctx: CoreCtx, segundos: int) -> CoreCtx:
    """El mismo contexto con otro cooldown, conservando el estado (que es lo que guarda la
    marca del último SMS)."""
    import dataclasses

    return dataclasses.replace(ctx, wake_cooldown_s=segundos, state=ctx.state)
