"""Tests de `core/pin_lockout.py` — el anti-bloqueo del PIN de comandos.

El módulo usa `time.time()` para la ventana deslizante, así que todo lo temporal va con el
fixture `freezer` (freezegun), igual que en HA core.
"""

from __future__ import annotations

from freezegun.api import FrozenDateTimeFactory
import pytest

from custom_components.ebro.core.pin_lockout import PinLockedError, PinLockout


def _falla(lockout: PinLockout) -> None:
    with lockout.attempt() as attempt:
        attempt.record_failure()


def test_arranca_desbloqueado() -> None:
    lockout = PinLockout()
    assert lockout.is_locked() is False
    assert lockout.failed_attempts == 0


def test_bloquea_tras_max_fail() -> None:
    lockout = PinLockout(max_failures=2, window_s=600)

    _falla(lockout)
    assert lockout.failed_attempts == 1
    assert lockout.is_locked() is False

    _falla(lockout)
    assert lockout.failed_attempts == 2
    assert lockout.is_locked() is True


def test_intento_bloqueado_lanza_sin_entrar_en_el_bloque() -> None:
    """Cuando salta la protección NO se contacta con el backend: es la diferencia clave con un
    rechazo real, porque aquí no se gasta ningún intento del lado de Chery."""
    lockout = PinLockout(max_failures=1)
    _falla(lockout)

    entrado = False
    with pytest.raises(PinLockedError) as err, lockout.attempt():
        entrado = True

    assert entrado is False
    assert err.value.attempts == 1
    assert "1 intentos" in str(err.value)


def test_exito_pone_el_contador_a_cero() -> None:
    lockout = PinLockout(max_failures=2)
    _falla(lockout)

    with lockout.attempt() as attempt:
        attempt.success()

    assert lockout.failed_attempts == 0
    assert lockout.is_locked() is False


def test_intento_sin_declarar_no_cuenta() -> None:
    """Un error de red o un rechazo por permisos no son un PIN erróneo y no deben acercar el
    bloqueo de la cuenta. Por eso el resultado se declara a mano y no se deduce."""
    lockout = PinLockout(max_failures=2)

    with lockout.attempt():
        pass  # ninguna declaración

    assert lockout.failed_attempts == 0


def test_el_contador_se_actualiza_aunque_el_bloque_lance() -> None:
    """El caso que motiva el `finally`.

    El llamador declara `fallido()` y justo después lanza un `CommandError` con el remedio. Sin
    `finally`, la excepción se saltaría la actualización del contador y la protección de la
    cuenta quedaría desactivada en silencio.
    """
    lockout = PinLockout(max_failures=2)

    with pytest.raises(RuntimeError), lockout.attempt() as attempt:
        attempt.record_failure()
        raise RuntimeError("rechazo del backend")

    assert lockout.failed_attempts == 1


def test_ventana_deslizante_expira(freezer: FrozenDateTimeFactory) -> None:
    """Pasada `window_s` desde el ÚLTIMO error el bloqueo se levanta solo."""
    lockout = PinLockout(max_failures=2, window_s=600)
    _falla(lockout)
    _falla(lockout)
    assert lockout.is_locked() is True

    freezer.tick(599)
    assert lockout.is_locked() is True

    freezer.tick(2)  # 601 s > window_s
    assert lockout.is_locked() is False


def test_la_ventana_se_reinicia_con_el_siguiente_fallo(
    freezer: FrozenDateTimeFactory,
) -> None:
    """Es deslizante, no fija: expirada la ventana se puede reintentar, y un fallo nuevo
    vuelve a bloquear contando desde ese instante, no desde el primero.

    NB: mientras está bloqueado NO se puede intentar (`attempt()` lanza), así que el reinicio
    de la ventana solo es observable después de que expire.
    """
    lockout = PinLockout(max_failures=2, window_s=600)
    _falla(lockout)
    _falla(lockout)

    freezer.tick(601)  # la ventana expira → se permite reintentar
    assert lockout.is_locked() is False

    _falla(lockout)  # vuelve a fallar: bloquea otra vez, con el reloj reiniciado
    assert lockout.is_locked() is True

    freezer.tick(599)
    assert lockout.is_locked() is True  # 1200 s desde el primero, solo 599 desde el último

    freezer.tick(2)
    assert lockout.is_locked() is False


def test_reset_desbloquea_de_inmediato() -> None:
    """El usuario reconfigura el PIN (config flow o Repair) AUNQUE reintroduzca el mismo: sin
    reset se quedaría parado, sin ninguna señal, hasta que venciera la ventana."""
    lockout = PinLockout(max_failures=1)
    _falla(lockout)
    assert lockout.is_locked() is True

    lockout.reset()

    assert lockout.is_locked() is False
    assert lockout.failed_attempts == 0


def test_instancias_independientes() -> None:
    """Con dos coches configurados los errores de uno no deben bloquear los comandos del otro:
    por eso la clase es instanciable y no un contador de proceso."""
    coche_a = PinLockout(max_failures=1)
    coche_b = PinLockout(max_failures=1)

    _falla(coche_a)

    assert coche_a.is_locked() is True
    assert coche_b.is_locked() is False
