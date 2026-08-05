"""Tests de `core/ebro_login.py` — login OAuth2 teléfono+contraseña contra el BFF."""

from __future__ import annotations

import base64
from unittest.mock import MagicMock, patch

import pytest
from syrupy.assertion import SnapshotAssertion

from custom_components.ebro.core import ebro_login


def _respuesta(json_data=None, *, status: int = 200, text: str = "") -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.text = text
    if json_data is None:
        r.json.side_effect = ValueError("no es JSON")
    else:
        r.json.return_value = json_data
    return r


# ───────────────────────── cifrado de la contraseña (puro) ─────────────────────────


def test_aes_encrypt_password_es_determinista(snapshot: SnapshotAssertion) -> None:
    """AES-CBC con key == IV fijos y sin nonce → mismo texto, misma salida siempre.

    Es una propiedad del protocolo, no una casualidad: el backend espera exactamente este
    valor. Congelarlo en un snapshot detecta cualquier cambio de clave o de padding.
    """
    cifrada = ebro_login.aes_encrypt_password("contrasena-de-prueba")

    assert cifrada == ebro_login.aes_encrypt_password("contrasena-de-prueba")
    assert cifrada == snapshot


def test_aes_encrypt_password_es_base64_y_multiplo_de_bloque() -> None:
    crudo = base64.b64decode(ebro_login.aes_encrypt_password("hola"))
    assert len(crudo) % 16 == 0


def test_aes_encrypt_password_soporta_no_ascii() -> None:
    """El padding es sobre los BYTES utf-8, no sobre los caracteres."""
    assert base64.b64decode(ebro_login.aes_encrypt_password("contraseña-ñ"))


# ───────────────────────── cabeceras (puras) ─────────────────────────


def test_headers(snapshot: SnapshotAssertion) -> None:
    assert ebro_login._headers("34") == snapshot


def test_headers_el_area_code_va_en_dept_id() -> None:
    assert ebro_login._headers("39")["dept-id"] == "39"
    # el tenant es del login (numérico) y NO depende del país
    assert ebro_login._headers("39")["tenant-code"] == ebro_login.TENANT_CODE


# ───────────────────────── normalizador de la respuesta (puro) ─────────────────────────


def test_extract_desde_data() -> None:
    tok = ebro_login._extract(
        {"data": {"access_token": "AT", "refresh_token": "RT", "expires_in": 43200}}
    )
    assert tok["access_token"] == "AT"
    assert tok["refresh_token"] == "RT"
    assert tok["expires_in"] == 43200


def test_extract_desde_la_raiz() -> None:
    """El BFF responde a veces con el token en el nivel superior y no bajo `data`."""
    tok = ebro_login._extract({"access_token": "AT"})
    assert tok["access_token"] == "AT"
    assert tok["refresh_token"] is None


def test_extract_conserva_el_json_crudo() -> None:
    """`raw` es lo que el config flow escribe en token.json: no puede perderse."""
    j = {"access_token": "AT", "extra": "valor"}
    assert ebro_login._extract(j)["raw"] is j


@pytest.mark.parametrize(
    "j",
    [{}, {"data": {}}, {"msg": "credenciales erróneas"}, {"data": {"access_token": ""}}],
)
def test_extract_sin_token(j) -> None:
    assert ebro_login._extract(j) is None


# ───────────────────────── password_login (red mockeada) ─────────────────────────


def test_password_login_ok() -> None:
    resp = _respuesta({"data": {"access_token": "AT", "refresh_token": "RT"}})

    with patch.object(ebro_login.requests, "post", return_value=resp) as post:
        ok, tok = ebro_login.password_login("600000000", "secreta", area_code="34")

    assert ok is True
    assert tok["access_token"] == "AT"

    _, kwargs = post.call_args
    # username = "<areaCode>_<mobile>" y grant_type/scope van en QUERY STRING
    assert kwargs["params"] == {
        "username": "34_600000000",
        "grant_type": "password",
        "scope": "server",
    }
    # la contraseña viaja cifrada en el BODY, nunca en claro
    assert kwargs["data"] == {"password": ebro_login.aes_encrypt_password("secreta")}
    assert "secreta" not in str(kwargs["data"])


def test_password_login_credenciales_erroneas() -> None:
    resp = _respuesta({"msg": "usuario o contraseña incorrectos"}, status=200)

    with patch.object(ebro_login.requests, "post", return_value=resp):
        ok, detalle = ebro_login.password_login("600000000", "mala")

    assert ok is False
    assert "usuario o contraseña incorrectos" in detalle


def test_password_login_respuesta_no_json() -> None:
    """Un gateway caído devuelve HTML: no debe propagar la excepción del parser."""
    resp = _respuesta(None, status=502, text="<html>Bad Gateway</html>")

    with patch.object(ebro_login.requests, "post", return_value=resp):
        ok, detalle = ebro_login.password_login("600000000", "x")

    assert ok is False
    assert detalle.startswith("HTTP 502:")


def test_refresh_token_ok() -> None:
    resp = _respuesta({"access_token": "AT2", "refresh_token": "RT2"})

    with patch.object(ebro_login.requests, "post", return_value=resp) as post:
        ok, tok = ebro_login.refresh_token("RT1")

    assert ok is True
    assert tok["access_token"] == "AT2"
    # el refresco NO manda contraseña: solo el grant en query string
    _, kwargs = post.call_args
    assert kwargs["params"]["grant_type"] == "refresh_token"
    assert kwargs["params"]["refresh_token"] == "RT1"
    assert "data" not in kwargs


def test_refresh_token_rechazado() -> None:
    resp = _respuesta({"error": "invalid_grant"}, status=400)

    with patch.object(ebro_login.requests, "post", return_value=resp):
        ok, detalle = ebro_login.refresh_token("caducado")

    assert ok is False
    assert "invalid_grant" in detalle
