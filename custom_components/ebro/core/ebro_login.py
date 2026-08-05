"""ebro_login.py — Login y refresco del token para la plataforma Chery "legend" (marca Ebro, EU).

Receta VERIFICADA de extremo a extremo (2026-07-27) capturando el login real de la app
(reFlutter+WireGuard) y replicándola desde Python (HTTP 200, access_token+refresh_token).
AUTÓNOMO: el usuario inicia sesión una vez con teléfono+contraseña, luego el token se renueva
solo con el refresh_token (< 12h de validez).

Constantes VERIFICADAS (POST https://legend.ebroauto.com/api/auth/oauth2/token):
  - cliente OAuth : Basic base64("legendApp:legendApp")
  - tenant-code / tenant-id : "3000010"   (NB: NO "euebro" — euebro es solo el x-TenantId del TSP)
  - dept-id : "34" (código de país), client-toc : "Y", version : "1.0.11", agent : "android"
  - NINGUNA firma de gateway en oauth2/token (nada de signature/nonce/url/keys)
  - password : AES-CBC/PKCS7, key = IV = "w9R8Ag1KiL0pvMHc", base64 → enviada en el BODY (password=..)
  - username : "<areaCode>_<mobile>"  ej. "34_637929347"
  - grant_type / scope en QUERY STRING
"""
from __future__ import annotations

import base64

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
import requests

from .http import WRITE_TIMEOUT_S

LOGIN_HOST = "https://legend.ebroauto.com/api"
OAUTH_PATH = "/auth/oauth2/token"
CLIENT_BASIC = "Basic " + base64.b64encode(b"legendApp:legendApp").decode()

TENANT_CODE = "3000010"            # tenant del login (numérico) — distinto del x-TenantId TSP "euebro"
_PWD_KEY = b"w9R8Ag1KiL0pvMHc"     # clave AES-128 == IV (paquete Dart `encrypt`, modo CBC)
APP_VERSION = "1.0.11"

# Trozo del cuerpo que se incluye en el mensaje de error cuando la respuesta no es JSON:
# suficiente para ver qué contestó el servidor, sin volcar una página entera en el log.
_ERROR_SNIPPET_LEN = 150


def aes_encrypt_password(plaintext: str) -> str:
    """Replica EncryptUtils.aesEncrypt: AES-CBC/PKCS7(key=IV=_PWD_KEY) → base64."""
    c = AES.new(_PWD_KEY, AES.MODE_CBC, _PWD_KEY)
    return base64.b64encode(c.encrypt(pad(plaintext.encode("utf-8"), 16))).decode()


def _headers(area_code: str = "34") -> dict:
    return {
        "Authorization": CLIENT_BASIC,
        "content-type": "application/x-www-form-urlencoded",
        "user-agent": "Dart/3.10 (dart:io)",
        "accept": "application/json, text/plain, */*",
        "accept-language": "es-ES",
        "tenant-code": TENANT_CODE,
        "tenant-id": TENANT_CODE,
        "dept-id": area_code,
        "client-toc": "Y",
        "version": APP_VERSION,
        "agent": "android",
    }


def _extract(j: dict) -> dict | None:
    d = j.get("data") if isinstance(j.get("data"), dict) else j
    at = d.get("access_token")
    if not at:
        return None
    return {"access_token": at, "refresh_token": d.get("refresh_token"),
            "token_type": d.get("token_type"), "expires_in": d.get("expires_in"), "raw": j}


def _oauth_post(params: dict, *, body: dict | None = None, area_code: str, host: str,
                error_key: str) -> tuple[bool, dict | str]:
    """El POST a `oauth2/token`, su parseo y la extracción del token.

    Los dos grants (contraseña y refresh_token) comparten endpoint, cabeceras, manejo del
    cuerpo no-JSON y forma del error: solo cambian los parámetros del query string, si hay
    body, y bajo qué clave viene el detalle del fallo (`key` en el login, `error` en el
    refresco). Estaban escritos dos veces enteros.
    """
    # `data` solo cuando hay cuerpo: el grant de refresco NO lleva ninguno, y pasarlo como
    # `None` — aunque `requests` lo trate igual — borraría esa garantía de la llamada.
    extra = {"data": body} if body is not None else {}
    r = requests.post(host + OAUTH_PATH, params=params, headers=_headers(area_code),
                      timeout=WRITE_TIMEOUT_S, **extra)
    try:
        j = r.json()
    except Exception:
        return False, f"HTTP {r.status_code}: {r.text[:_ERROR_SNIPPET_LEN]}"
    tok = _extract(j)
    if tok:
        return True, tok
    return False, f"HTTP {r.status_code}: {j.get('msg') or j.get(error_key) or j}"


def password_login(mobile: str, password: str, area_code: str = "34",
                   host: str = LOGIN_HOST) -> tuple[bool, dict | str]:
    """Login teléfono+contraseña. `mobile` = solo el número; `area_code` ej. "34" (España).
    Devuelve (True, {access_token, refresh_token,...}) o (False, mensaje)."""
    return _oauth_post(
        {"username": f"{area_code}_{mobile}", "grant_type": "password", "scope": "server"},
        body={"password": aes_encrypt_password(password)},
        area_code=area_code, host=host, error_key="key")


def refresh_token(refresh_tok: str, area_code: str = "34",
                  host: str = LOGIN_HOST) -> tuple[bool, dict | str]:
    """Renueva con el grant refresh_token (sin contraseña). Devuelve nuevo access+refresh."""
    return _oauth_post(
        {"grant_type": "refresh_token", "refresh_token": refresh_tok, "scope": "server"},
        area_code=area_code, host=host, error_key="error")
