"""Tests de `core/session.py` — salud del token (keep-alive de la sesión).

El módulo solo orquesta: toda su lógica interesante está en cuatro costuras hacia `wake`.
Mockeándolas queda un módulo efectivamente puro, y la rama que de verdad importa es
**EXPIRED vs NET_ERROR**: es la que decide si a un usuario le aparece la tarjeta
«Reautenticar». Confundirlas ante un corte de red le haría reautenticar en balde.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from custom_components.ebro.core import session


def _ctx(*, refresh_reason: str = "", refresh_ts: float | None = None):
    """Contexto mínimo: `check()` solo mira `estado.refresh_motivo` y `estado.refresh_ts`."""
    return SimpleNamespace(
        state=SimpleNamespace(
            refresh_reason=refresh_reason,
            refresh_ts=time.time() if refresh_ts is None else refresh_ts,
        )
    )


# ───────────────────────── check() ─────────────────────────


def test_check_ok() -> None:
    with patch.object(session.wake, "_bff_login", return_value=("token", "tuser")):
        ok, detalle, status = session.check(_ctx())

    assert (ok, status) == (True, session.STATUS_OK)
    assert "activa" in detalle


def test_check_excepcion_es_error_de_red_no_sesion_caducada() -> None:
    """Una excepción del login (DNS, TLS, timeout) NO es una sesión muerta."""
    with patch.object(session.wake, "_bff_login", side_effect=OSError("dns")):
        ok, detalle, status = session.check(_ctx())

    assert ok is False
    assert status == session.STATUS_NET_ERROR
    assert "OSError" in detalle


def test_check_login_fallido_sin_marcador_es_expired() -> None:
    """Sin ninguna señal de red, un login rechazado se interpreta como sesión muerta."""
    with patch.object(session.wake, "_bff_login", return_value=(None, None)):
        ok, _, status = session.check(_ctx())

    assert ok is False
    assert status == session.STATUS_EXPIRED


def test_check_login_fallido_con_marcador_de_red_reciente_es_net_error() -> None:
    """Si la RENOVACIÓN ni siquiera arrancó por red, la sesión puede seguir viva al otro lado.

    Declararla caducada haría aparecer «Reautenticar» sin motivo — el caso que motiva el
    marcador `red:` y su ventana de frescura.
    """
    ctx = _ctx(refresh_reason="red:ConnectTimeout")

    with patch.object(session.wake, "_bff_login", return_value=(None, None)):
        ok, detalle, status = session.check(ctx)

    assert ok is False
    assert status == session.STATUS_NET_ERROR
    assert "ConnectTimeout" in detalle


def test_check_ignora_un_marcador_de_red_viejo() -> None:
    """Un marcador anterior a `_MOTIVO_RECIENTE_S` habla de otra vuelta, no de esta."""
    ctx = _ctx(
        refresh_reason="red:ConnectTimeout",
        refresh_ts=time.time() - session._REASON_FRESH_S - 1,
    )

    with patch.object(session.wake, "_bff_login", return_value=(None, None)):
        _, _, status = session.check(ctx)

    assert status == session.STATUS_EXPIRED


def test_check_marcador_reciente_pero_no_de_red_es_expired() -> None:
    """Solo `red:` indica red. Un `rechazado:*` es un rechazo real → sesión muerta."""
    ctx = _ctx(refresh_reason="rechazado:invalid_grant")

    with patch.object(session.wake, "_bff_login", return_value=(None, None)):
        _, _, status = session.check(ctx)

    assert status == session.STATUS_EXPIRED


def test_check_sin_marcador_de_red_declara_la_sesion_caducada() -> None:
    """Sin un marcador RECIENTE de «la renovación no salió por red», un login fallido es una
    sesión muerta y toca reautenticar.

    Antes este test pasaba un `SimpleNamespace()` vacío para ejercitar un `try/except` que
    toleraba contextos sin `state`. Esos contextos eran las herramientas de diagnóstico por
    línea de comandos, borradas en la primera pasada: el guarda ya no protegía de nada y, si
    algún día hubiera saltado, se habría tragado un AttributeError real."""
    from custom_components.ebro.core.context import CoreCtx

    with patch.object(session.wake, "_bff_login", return_value=(None, None)):
        _, _, status = session.check(CoreCtx())

    assert status == session.STATUS_EXPIRED


# ───────────────────────── refresh() ─────────────────────────


@pytest.mark.parametrize(("devuelto", "esperado"), [(True, True), (None, False), (0, False)])
def test_refresh(devuelto, esperado) -> None:
    with patch.object(session.wake, "_refresh_token", return_value=devuelto):
        assert session.refresh(_ctx()) is esperado


def test_refresh_nunca_lanza() -> None:
    with patch.object(session.wake, "_refresh_token", side_effect=RuntimeError("boom")):
        assert session.refresh(_ctx()) is False


# ───────────────────────── refresh_si_proximo_a_caducar() ─────────────────────────


def test_proactivo_no_hace_falta_todavia() -> None:
    """A mitad de vida no se renueva y NO se hace ninguna llamada."""
    with (
        patch.object(session.wake, "_eta_token", return_value=(6 * 3600, 12 * 3600)),
        patch.object(session.wake, "_refresh_token_detail") as detalle,
    ):
        renewed, reason = session.refresh_if_expiring(_ctx())

    assert (renewed, reason) == (False, "no_hace_falta")
    detalle.assert_not_called()


def test_proactivo_renueva_al_superar_la_cuota() -> None:
    """Pasada la cuota (0.8 = a las 9h36m de 12h) se renueva por adelantado."""
    with (
        patch.object(session.wake, "_eta_token", return_value=(10 * 3600, 12 * 3600)),
        patch.object(
            session.wake, "_refresh_token_detail", return_value=(True, "")
        ) as detalle,
    ):
        renewed, reason = session.refresh_if_expiring(_ctx())

    assert (renewed, reason) == (True, "")
    detalle.assert_called_once()


@pytest.mark.parametrize("eta_duracion", [(-1, 12 * 3600), (100, 0)])
def test_proactivo_no_determinable(eta_duracion) -> None:
    """Sin token.json (o con un mtime imposible) no se decide nada."""
    with patch.object(session.wake, "_eta_token", return_value=eta_duracion):
        assert session.refresh_if_expiring(_ctx()) == (False, "no_determinable")


def test_proactivo_nunca_lanza() -> None:
    """Es una optimización: no puede romper el keep-alive que la invoca."""
    with patch.object(session.wake, "_eta_token", side_effect=OSError("stat")):
        renewed, reason = session.refresh_if_expiring(_ctx())

    assert renewed is False
    assert reason == "red:OSError"
