"""Tests de `core/routing.py` — la tabla única de enrutado de códigos del backend."""

from __future__ import annotations

from dataclasses import asdict

import pytest
from syrupy.assertion import SnapshotAssertion

from custom_components.ebro.core import routing


def test_matriz_completa(snapshot: SnapshotAssertion) -> None:
    """Snapshot de TODA la matriz código × contexto.

    `clasifica()` es determinista y sin reloj, así que la matriz entera cabe en un snapshot.
    Es el test de más valor del módulo: cualquier cambio en `_TABLA`, en los overrides o en
    los defaults sale como un diff legible en vez de como un remedio equivocado en producción.
    """
    codigos = sorted(set(routing._TABLE) | set(routing._OVERRIDE_CHECKPASSWORD))
    # códigos que la tabla NO conoce: la rama por defecto, que es asimétrica por contexto
    codigos += ["A99999", "", "123"]

    matriz = {}
    for code in codigos:
        for context in (routing.CONTEXT_CHECKPASSWORD, routing.CONTEXT_COMMAND):
            c = routing.classify(code, context)
            matriz[f"{code or '<vacio>'}|{context}"] = {
                **asdict(c),
                "accion": c.action,
                "exito": c.success,
                "fallo": c.failed,
            }

    assert matriz == snapshot


def test_default_asimetrico_por_contexto() -> None:
    """Un código desconocido es PIN+bloqueante en checkPassword y no bloqueante en comando.

    La asimetría es deliberada (ver el docstring del módulo) y fácil de romper sin darse
    cuenta al tocar los defaults, así que se afirma explícitamente además del snapshot.
    """
    cp = routing.classify("A99999", routing.CONTEXT_CHECKPASSWORD)
    assert cp.reason == routing.REASON_PIN
    assert cp.counts_for_lockout is True
    assert cp.action == routing.ACTION_REPAIR_PIN

    cmd = routing.classify("A99999", routing.CONTEXT_COMMAND)
    assert cmd.reason is routing.REASON_NONE
    assert cmd.counts_for_lockout is False
    assert cmd.outcome == "unknown"
    assert cmd.action == routing.ACTION_NOTICE


def test_a00567_cambia_de_significado_segun_contexto() -> None:
    """El caso que motivó pasar el contexto a `clasifica()`.

    En checkPassword `A00567` es una petición incompleta (el PIN puede estar bien); en
    respuesta a un comando es un taskId caducado que hay que regenerar.
    """
    cp = routing.classify("A00567", routing.CONTEXT_CHECKPASSWORD)
    assert cp.reason == routing.REASON_CONFIG
    assert cp.regenerate_taskid is False
    assert cp.counts_for_lockout is False

    cmd = routing.classify("A00567", routing.CONTEXT_COMMAND)
    assert cmd.regenerate_taskid is True
    assert cmd.outcome == "ko"


@pytest.mark.parametrize(
    ("code", "esperado"),
    [
        (None, None),
        ("", None),
        (123, "123"),
        ("A00082", "A00082"),
    ],
)
def test_code_normalizado(code, esperado) -> None:
    """`code` puede llegar como None o no-cadena (respuesta ilegible del backend)."""
    assert routing.classify(code, routing.CONTEXT_COMMAND).code == esperado


def test_solo_los_codigos_de_pin_cuentan_para_el_bloqueo() -> None:
    """Contar errores de permisos/config acercaría el bloqueo real de la cuenta por una causa
    que con el PIN no tiene nada que ver — es el bug P1-2 que documenta el módulo.

    La lista es EXHAUSTIVA a propósito: añadir un código aquí significa decidir que puede gastar
    intentos de la cuenta del usuario, y eso merece pasar por este test y no colarse por la regla
    por defecto. A07908/A07909 entraron el 2026-10-01 tras confirmarse en campo que son un PIN
    erróneo de verdad."""
    bloqueantes = {
        code
        for code in set(routing._TABLE) | set(routing._OVERRIDE_CHECKPASSWORD)
        if routing.classify(code, routing.CONTEXT_CHECKPASSWORD).counts_for_lockout
    }
    assert bloqueantes == {"A00285", "A00282", "A07908", "A07909"}


@pytest.mark.parametrize(
    ("reason", "action"),
    [
        (routing.REASON_REAUTH, routing.ACTION_REAUTH),
        (routing.REASON_PIN, routing.ACTION_REPAIR_PIN),
        (routing.REASON_CONFIG, routing.ACTION_NOTICE),
        (routing.REASON_NONE, routing.ACTION_NOTICE),
        ("un-reason-que-no-existe", routing.ACTION_NOTICE),
    ],
)
def test_accion_por_reason(reason, action) -> None:
    """Un `reason` desconocido degrada a aviso: nunca a reauth ni a un Repair inaccionable."""
    assert routing.action_for_reason(reason) == action


def test_vistas_derivadas_coinciden_con_la_tabla() -> None:
    """Los frozenset públicos se derivan de `_TABLA`; antes eran listas paralelas que podían
    divergir en silencio."""
    assert {"000000", "A00079"} == routing.SUCCESS_CODES
    assert {"A00082"} == routing.RETRYABLE_CODES
    assert {"A00089", "A00546", "A00567"} == routing.TASKID_INVALID
    assert routing.SUCCESS_CODES.isdisjoint(routing.FAILURE_CODES)


def test_la_familia_a079_de_checkpassword_SI_es_el_pin() -> None:
    """Confirmado en campo el 2026-10-01: con el PIN correcto el mismo comando salió a la
    primera, en la misma cuenta y tras toda una mañana dando A07908/A07909.

    Durante unas horas estuvieron clasificados al revés, con un razonamiento que parecía sólido
    —la app aceptaba el PIN que a nosotros nos fallaba— y una premisa falsa: la app estaba
    aceptando el PIN NUEVO de otra cuenta.

    Esta prueba existe porque equivocarse aquí no lo paga el software, lo paga la cuenta del
    usuario: sin `counts_for_lockout` el anti-bloqueo deja de frenar un PIN realmente erróneo y
    Chery acaba bloqueando la cuenta."""
    for code in ("A07908", "A07909"):
        resultado = routing.classify(code, routing.CONTEXT_CHECKPASSWORD)

        assert resultado.counts_for_lockout is True, code
        assert resultado.reason == routing.REASON_PIN, code


def test_los_permisos_del_vehiculo_no_cuentan_para_el_bloqueo() -> None:
    """La otra mitad, que sigue en pie: un rechazo por permisos o por petición malformada no
    debe acercar el bloqueo de la cuenta por una causa ajena al PIN."""
    for code in ("A00374", "A00554", "A00757"):
        resultado = routing.classify(code, routing.CONTEXT_CHECKPASSWORD)

        assert resultado.counts_for_lockout is False, code
        assert resultado.reason == routing.REASON_CONFIG, code
