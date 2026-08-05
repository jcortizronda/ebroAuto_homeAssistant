"""Tests de `core/commands.py` — catálogo, taskId y envío de comandos.

Dos detalles del módulo condicionan cómo se mockea:

* `send()` usa **urllib**, mientras que `_checkpassword` importa **requests dentro de la
  función**. Por eso `patch("custom_components.ebro.core.commands.requests")` NO funciona:
  el módulo no tiene ese atributo. Hay que parchear `requests.post` global.
* `send()` mete `int(time.time()*1000)` en `seq` y en la firma → todo test que mire el
  envelope necesita el reloj congelado.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from syrupy.assertion import SnapshotAssertion

from custom_components.ebro.core import catalog, commands, routing, taskid
from custom_components.ebro.core.context import CoreCtx

FROZEN = "2026-01-15 12:00:00+00:00"


@pytest.fixture
def ctx(tmp_path) -> CoreCtx:
    return CoreCtx(
        vin="LSJA0000000000001",
        tuserid="tuser-0000000000",
        pin="1234",
        token_path=str(tmp_path / "token.json"),
        taskid_file=str(tmp_path / "taskid.txt"),
    )


def _urlopen(payload: dict, status: int = 200) -> MagicMock:
    """Fabrica el context manager que devuelve `urllib.request.urlopen`."""
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    resp.status = status
    cm = MagicMock()
    cm.__enter__.return_value = resp
    return cm


# ───────────────────────── catálogo (puro) ─────────────────────────


def test_catalogo_sin_claves_duplicadas() -> None:
    """`CMD_MAP = dict(COMMANDS)`: una clave repetida se tragaría un comando en silencio."""
    claves = [k for k, _ in catalog.COMMANDS]
    assert len(claves) == len(set(claves))
    assert len(catalog.CMD_MAP) == len(catalog.COMMANDS)


def test_cada_comando_tiene_destino_y_nombre() -> None:
    for key, spec in catalog.COMMANDS:
        assert spec.get("path") or spec.get("endpoint"), key
        assert spec.get("name"), key
        assert isinstance(spec.get("body"), dict), key


def test_command_error_marca_lo_reintentable() -> None:
    assert commands.CommandError("x", code="A00082").retryable is True
    assert commands.CommandError("x", code="A00084").retryable is False
    assert commands.CommandError("x").retryable is False


# ───────────────────────── mensajes (puros) ─────────────────────────


def test_mensajes_de_checkpassword(snapshot: SnapshotAssertion) -> None:
    """El texto está separado de la decisión a propósito: se puede reescribir o traducir sin
    tocar el enrutado. El snapshot fija los tres mensajes de golpe."""
    detail = "code=A00285 'wrong password'"
    assert {
        reason: taskid._checkpassword_message(
            routing.Classification(reason=reason), detail
        )
        for reason in (routing.REASON_REAUTH, routing.REASON_CONFIG, routing.REASON_PIN)
    } == snapshot


# ───────────────────────── get_taskid ─────────────────────────


def test_get_taskid_desde_archivo(ctx: CoreCtx) -> None:
    """Orden de las fuentes: archivo → caché → checkPassword."""
    with open(ctx.taskid_file, "w", encoding="utf-8") as fh:
        fh.write("  task-de-archivo  \n")

    assert taskid.get_taskid(ctx, "tu") == ("task-de-archivo", "file")


def test_get_taskid_desde_cache(ctx: CoreCtx, freezer) -> None:
    """Regenerarlo cuesta toda la vuelta de checkPassword, que es la parte lenta del comando."""
    freezer.move_to(FROZEN)
    ctx.state.taskid = "task-en-cache"
    ctx.state.taskid_ts = __import__("time").time()

    assert taskid.get_taskid(ctx, "tu") == ("task-en-cache", "cache")


def test_get_taskid_cache_caducada_regenera(ctx: CoreCtx, freezer) -> None:
    freezer.move_to(FROZEN)
    ctx.state.taskid = "task-viejo"
    ctx.state.taskid_ts = __import__("time").time()
    freezer.tick(ctx.taskid_ttl + 1)

    with patch.object(taskid, "_mint_taskid", return_value="task-nuevo"):
        tid, src = taskid.get_taskid(ctx, "tu")

    assert (tid, src) == ("task-nuevo", "checkPassword")
    assert ctx.state.taskid == "task-nuevo"


def test_get_taskid_force_mint_salta_archivo_y_cache(ctx: CoreCtx) -> None:
    """En el reintento tras un rechazo, reusar la misma fuente daría el mismo error."""
    with open(ctx.taskid_file, "w", encoding="utf-8") as fh:
        fh.write("task-de-archivo")

    with patch.object(taskid, "_mint_taskid", return_value="task-fresco") as mint:
        tid, src = taskid.get_taskid(ctx, "tu", force_mint=True)

    assert (tid, src) == ("task-fresco", "checkPassword")
    mint.assert_called_once()


def test_get_taskid_con_generacion_desactivada(ctx: CoreCtx) -> None:
    ctx.mint_taskid = False
    assert taskid.get_taskid(ctx, "tu") == (None, "none")


def test_get_taskid_propaga_el_command_error_con_su_reason(ctx: CoreCtx) -> None:
    """`send()` necesita el `reason` intacto para enrutar el remedio correcto."""
    err = commands.CommandError("PIN rechazado", reason="pin")

    with (
        patch.object(taskid, "_mint_taskid", side_effect=err),
        pytest.raises(commands.CommandError) as capt,
    ):
        taskid.get_taskid(ctx, "tu")

    assert capt.value.reason == "pin"


def test_get_taskid_error_imprevisto_se_envuelve_como_pin(ctx: CoreCtx) -> None:
    with (
        patch.object(taskid, "_mint_taskid", side_effect=ValueError("raro")),
        pytest.raises(commands.CommandError) as capt,
    ):
        taskid.get_taskid(ctx, "tu")

    assert capt.value.reason == "pin"


# ───────────────────────── _mint_taskid_impl ─────────────────────────


def test_pin_vacio_falla_antes_del_antibloqueo(ctx: CoreCtx) -> None:
    """Un PIN no configurado no es un intento erróneo: no debe consumir el umbral ni tocar el
    backend."""
    ctx.pin = "   "

    with pytest.raises(commands.CommandError) as capt:
        taskid._mint_taskid_impl(ctx, "tu")

    assert capt.value.reason == "pin"
    assert ctx.lockout.failed_attempts == 0


def test_antibloqueo_convierte_el_bloqueo_en_command_error(ctx: CoreCtx) -> None:
    ctx.lockout.max_failures = 1
    with ctx.lockout.attempt() as attempt:
        attempt.record_failure()

    with pytest.raises(commands.CommandError) as capt:
        taskid._mint_taskid_impl(ctx, "tu")

    assert capt.value.reason == "pin"
    assert "bloqueado temporalmente" in str(capt.value)


# ───────────────────────── send() ─────────────────────────


def test_send_comando_desconocido(ctx: CoreCtx) -> None:
    with pytest.raises(commands.CommandError, match="Comando desconocido"):
        commands.send(ctx, "no-existe-este-comando")


def test_send_sin_token_pide_reauth(ctx: CoreCtx) -> None:
    with (
        patch.object(commands.wake, "_bff_login", return_value=(None, None)),
        pytest.raises(commands.CommandError) as capt,
    ):
        commands.send(ctx, "bloquear")

    assert capt.value.reason == "reauth"


@pytest.mark.freeze_time(FROZEN)
def test_send_ok_y_envelope_firmado(ctx: CoreCtx, snapshot: SnapshotAssertion) -> None:
    """Con el reloj congelado, `seq` y la firma son reproducibles → el envelope entero cabe en
    un snapshot. Es la mejor red de seguridad contra un cambio accidental del formato."""
    ctx.state.taskid = "task-en-cache"
    ctx.state.taskid_ts = __import__("time").time()

    with (
        patch.object(commands.wake, "_bff_login", return_value=("UT", "tu")),
        patch("urllib.request.urlopen", return_value=_urlopen({"code": "000000"})) as urlopen,
    ):
        out = commands.send(ctx, "bloquear")

    assert "000000" in out

    req = urlopen.call_args[0][0]
    enviado = json.loads(req.data.decode())

    assert enviado["seq"] == f"{ctx.vin}-1768478400000"
    assert enviado["taskId"] == "task-en-cache"
    assert enviado["vin"] == ctx.vin
    assert enviado["clientType"] == "1"
    assert enviado == snapshot


@pytest.mark.freeze_time(FROZEN)
def test_send_aplica_los_params_antes_de_los_campos_de_sistema(ctx: CoreCtx) -> None:
    """Los `params` sobrescriben el body del catálogo, pero clientType/seq/taskId/vin los pone
    siempre `send()`: un `params` no puede falsificarlos."""
    ctx.state.taskid = "T"
    ctx.state.taskid_ts = __import__("time").time()

    with (
        patch.object(commands.wake, "_bff_login", return_value=("UT", "tu")),
        patch("urllib.request.urlopen", return_value=_urlopen({"code": "000000"})) as urlopen,
    ):
        commands.send(ctx, "clima_on", params={"temperature": "22.0", "vin": "VIN-FALSO"})

    enviado = json.loads(urlopen.call_args[0][0].data.decode())
    assert enviado["temperature"] == "22.0"
    assert enviado["vin"] == ctx.vin


@pytest.mark.freeze_time(FROZEN)
def test_send_codigo_de_fallo_lanza_command_error(ctx: CoreCtx) -> None:
    """Un fallo conocido = comando NO ejecutado → las entidades optimistas anulan el estado en
    vez de mostrar un falso éxito."""
    ctx.state.taskid = "T"
    ctx.state.taskid_ts = __import__("time").time()

    with (
        patch.object(commands.wake, "_bff_login", return_value=("UT", "tu")),
        patch("urllib.request.urlopen", return_value=_urlopen({"code": "A00084"})),
        pytest.raises(commands.CommandError) as capt,
    ):
        commands.send(ctx, "bloquear")

    assert capt.value.code == "A00084"
    assert capt.value.retryable is False


@pytest.mark.freeze_time(FROZEN)
def test_send_codigo_desconocido_no_bloquea(ctx: CoreCtx) -> None:
    """Prudencia deliberada: no se inventa un fallo que el backend no ha declarado."""
    ctx.state.taskid = "T"
    ctx.state.taskid_ts = __import__("time").time()

    with (
        patch.object(commands.wake, "_bff_login", return_value=("UT", "tu")),
        patch("urllib.request.urlopen", return_value=_urlopen({"code": "A99999"})),
    ):
        out = commands.send(ctx, "bloquear")

    assert "A99999" in out


@pytest.mark.freeze_time(FROZEN)
def test_send_regenera_el_taskid_y_reintenta_una_sola_vez(ctx: CoreCtx) -> None:
    """Sin este reintento el usuario vería un error por un taskId simplemente caducado."""
    ctx.state.taskid = "task-caducado"
    ctx.state.taskid_ts = __import__("time").time()
    respuestas = [_urlopen({"code": "A00089"}), _urlopen({"code": "000000"})]

    with (
        patch.object(commands.wake, "_bff_login", return_value=("UT", "tu")),
        patch.object(taskid, "_mint_taskid", return_value="task-nuevo") as mint,
        patch("urllib.request.urlopen", side_effect=respuestas) as urlopen,
    ):
        out = commands.send(ctx, "bloquear")

    assert "000000" in out
    assert urlopen.call_count == 2
    mint.assert_called_once()
    # el segundo envío lleva ya el taskId regenerado
    assert json.loads(urlopen.call_args[0][0].data.decode())["taskId"] == "task-nuevo"


@pytest.mark.freeze_time(FROZEN)
def test_send_no_reintenta_indefinidamente(ctx: CoreCtx) -> None:
    """Dos rechazos seguidos → se rinde con CommandError, no un bucle."""
    ctx.state.taskid = "T"
    ctx.state.taskid_ts = __import__("time").time()

    with (
        patch.object(commands.wake, "_bff_login", return_value=("UT", "tu")),
        patch.object(taskid, "_mint_taskid", return_value="task-nuevo"),
        patch("urllib.request.urlopen", return_value=_urlopen({"code": "A00089"})) as urlopen,
        pytest.raises(commands.CommandError),
    ):
        commands.send(ctx, "bloquear")

    assert urlopen.call_count == 2


@pytest.mark.freeze_time(FROZEN)
def test_send_lee_el_cuerpo_de_un_http_error(ctx: CoreCtx) -> None:
    """El backend manda el `code` real también en las respuestas 4xx."""
    import urllib.error

    ctx.state.taskid = "T"
    ctx.state.taskid_ts = __import__("time").time()
    err = urllib.error.HTTPError("url", 400, "Bad Request", {}, None)
    err.read = lambda: json.dumps({"code": "A00084"}).encode()

    with (
        patch.object(commands.wake, "_bff_login", return_value=("UT", "tu")),
        patch("urllib.request.urlopen", side_effect=err),
        pytest.raises(commands.CommandError) as capt,
    ):
        commands.send(ctx, "bloquear")

    assert capt.value.code == "A00084"


@pytest.mark.freeze_time(FROZEN)
def test_send_error_de_red(ctx: CoreCtx) -> None:
    ctx.state.taskid = "T"
    ctx.state.taskid_ts = __import__("time").time()

    with (
        patch.object(commands.wake, "_bff_login", return_value=("UT", "tu")),
        patch("urllib.request.urlopen", side_effect=OSError("sin ruta al host")),
        pytest.raises(commands.CommandError, match="Error de red"),
    ):
        commands.send(ctx, "bloquear")


# ───────────────────────── query_theft_switch ─────────────────────────


@pytest.mark.parametrize(
    ("respuesta", "esperado"),
    [
        ({"body": {"theftAlarmSwitch": 1}}, 1),
        ({"body": {"theftAlarmSwitch": "0"}}, 0),
        ({"body": {}}, None),
        ({"body": {"theftAlarmSwitch": "no-numero"}}, None),
        ({}, None),
        ("no es un dict", None),
    ],
)
def test_query_theft_switch(ctx: CoreCtx, respuesta, esperado) -> None:
    with (
        patch.object(commands.wake, "_bff_login", return_value=("UT", "tu")),
        patch.object(commands.wake, "_signed_post", return_value=(200, respuesta)),
    ):
        assert commands.query_theft_switch(ctx) == esperado


def test_query_theft_switch_sin_token(ctx: CoreCtx) -> None:
    with patch.object(commands.wake, "_bff_login", return_value=(None, None)):
        assert commands.query_theft_switch(ctx) is None


def test_query_theft_switch_no_lanza(ctx: CoreCtx) -> None:
    """Lo llama `async_added_to_hass` del switch de la alarma: no puede tumbar el setup."""
    with (
        patch.object(commands.wake, "_bff_login", return_value=("UT", "tu")),
        patch.object(commands.wake, "_signed_post", side_effect=OSError("red")),
    ):
        assert commands.query_theft_switch(ctx) is None
