"""Tests de `core/ebro_auth.py` — firma de la app + codificador SM4.

Todo el cripto de este módulo es determinista (SM4-ECB con clave fija, SHA-256), así que
vale un snapshot directo. La ÚNICA fuente de no determinismo es el timestamp por defecto de
`sign_post`, que se cierra pasando `ts_ms` explícito o congelando el reloj.
"""

from __future__ import annotations

import hashlib

from freezegun.api import FrozenDateTimeFactory
import pytest
from syrupy.assertion import SnapshotAssertion

from custom_components.ebro.core import ebro_auth
from custom_components.ebro.core.context import CoreCtx

TS = 1700000000000


# ───────────────────────── SM4 (determinista) ─────────────────────────


def test_sm4_vectores(snapshot: SnapshotAssertion) -> None:
    """SM4-ECB con clave fija y sin IV: mismo texto → misma salida, siempre."""
    assert {
        "plain": ebro_auth.sm4_code("1234"),
        "padRight32": ebro_auth.sm4_code("1234", "padRight32"),
        "padLeft32": ebro_auth.sm4_code("1234", "padLeft32"),
        "bloque_exacto": ebro_auth.sm4_ecb_encrypt_pkcs7(b"0123456789abcdef").hex(),
    } == snapshot


def test_sm4_es_estable_entre_llamadas() -> None:
    assert ebro_auth.sm4_code("1234") == ebro_auth.sm4_code("1234")


def test_sm4_pkcs7_anade_un_bloque_entero_si_ya_es_multiplo() -> None:
    """PKCS#7 canónico: 16 bytes de entrada → 32 de salida, no 16."""
    assert len(ebro_auth.sm4_ecb_encrypt_pkcs7(b"0123456789abcdef")) == 32
    assert len(ebro_auth.sm4_ecb_encrypt_pkcs7(b"corto")) == 16


@pytest.mark.parametrize(
    ("transform", "longitud"),
    [("plain", 4), ("padRight32", 32), ("padLeft32", 32)],
)
def test_sm4_transformaciones_de_relleno(transform, longitud) -> None:
    """El relleno cambia el texto claro, luego también el cifrado."""
    s = "1234"
    esperado = {"plain": s, "padRight32": s.ljust(32), "padLeft32": s.rjust(32)}[transform]
    assert len(esperado) == longitud
    assert ebro_auth.sm4_code(s, transform) == ebro_auth.sm4_code(esperado)


def test_sm4_acepta_no_cadenas() -> None:
    assert ebro_auth.sm4_code(1234) == ebro_auth.sm4_code("1234")


# ───────────────────────── sign_post ─────────────────────────


def test_sign_post_con_timestamp_explicito() -> None:
    """SHA256(secret + nonce + url + ts) en hex minúscula."""
    sig, ts = ebro_auth.sign_post("/tsp/v1/app/auth/login", ts_ms=TS)

    esperado = hashlib.sha256(
        f"{ebro_auth.SIGN_SECRET}{ebro_auth.SIGN_NONCE}/tsp/v1/app/auth/login{TS}".encode()
    ).hexdigest()

    assert ts == TS
    assert sig == esperado
    assert sig == sig.lower()
    assert len(sig) == 64


def test_sign_post_usa_el_reloj_si_no_hay_ts(freezer: FrozenDateTimeFactory) -> None:
    """Es la única no-determinación del módulo: por eso los tests de `headers_post`
    congelan el reloj."""
    freezer.move_to("2026-01-15 12:00:00+00:00")
    _, ts = ebro_auth.sign_post("/x")

    assert ts == 1768478400000


def test_sign_post_depende_de_la_ruta_y_del_ts() -> None:
    base, _ = ebro_auth.sign_post("/a", ts_ms=TS)
    assert ebro_auth.sign_post("/b", ts_ms=TS)[0] != base
    assert ebro_auth.sign_post("/a", ts_ms=TS + 1)[0] != base


# ───────────────────────── headers_post ─────────────────────────


@pytest.mark.freeze_time("2026-01-15 12:00:00+00:00")
def test_headers_post(snapshot: SnapshotAssertion) -> None:
    """`headers_post` nunca recibe `ts_ms`, así que aquí el reloj congelado es obligatorio."""
    assert ebro_auth.headers_post("/tsp/v1/app/vmc/queryList") == snapshot


@pytest.mark.freeze_time("2026-01-15 12:00:00+00:00")
def test_headers_post_toma_la_region_del_contexto() -> None:
    """Dos entradas con regiones distintas no deben pisarse: por eso `ctx` gana sobre los
    valores por defecto del módulo, que solo sirven al diagnóstico por línea de comandos."""
    ctx = CoreCtx(channel_id="9", country_id="7", tenant_code="9999999")

    h = ebro_auth.headers_post("/x", ctx=ctx)

    assert h["channelId"] == "9"
    assert h["countryId"] == "7"
    assert h["TENANT-ID"] == "9999999"
    assert h["TENANT-CODE"] == "9999999"


@pytest.mark.freeze_time("2026-01-15 12:00:00+00:00")
def test_headers_post_sin_contexto_usa_los_defaults_del_modulo() -> None:
    h = ebro_auth.headers_post("/x")

    assert h["channelId"] == ebro_auth.CHANNEL_ID
    assert h["TENANT-CODE"] == ebro_auth.TENANT_CODE


@pytest.mark.freeze_time("2026-01-15 12:00:00+00:00")
def test_headers_post_dept_id_es_el_prefijo_de_pais() -> None:
    assert ebro_auth.headers_post("/x")["DEPT-ID"] == "34"
    assert ebro_auth.headers_post("/x", dept_id="39")["DEPT-ID"] == "39"


def test_constantes_de_region_se_leen_en_tiempo_de_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`BFF`/`TENANT_CODE`/`CHANNEL_ID` se resuelven del entorno al importar el módulo.

    Consecuencia: `monkeypatch.setenv` no sirve de nada después; hay que parchear el
    atributo. Se afirma para que quien escriba tests nuevos no pierda una tarde con esto.
    """
    monkeypatch.setenv("EBRO_TENANT_CODE", "0000000")

    import importlib

    assert ebro_auth.TENANT_CODE != "0000000"
    # y parcheando el atributo sí cambia
    monkeypatch.setattr(ebro_auth, "TENANT_CODE", "0000000")
    assert ebro_auth.headers_post("/x")["TENANT-CODE"] == "0000000"
    del importlib
