#!/usr/bin/env python3
"""Las dos formas de hablar con el backend, y sus timeouts.

Hay exactamente dos canales, y confundirlos no funciona:

* **BFF** (`legend.ebroauto.com`) — login, PIN y lista de vehículos. Autenticación por
  `Bearer <access_token>` y cabeceras firmadas de gateway (`ebro_auth.headers_post`).
* **TSP** (`tspconsole-*.ebroauto.com`) — telemetría y comandos. Autenticación por `userToken`
  y **cuerpo firmado** (`tsp_sign.sign_body`), que es otra firma distinta.

Estaban escritos a mano en `wake`, `commands`, `probe` y hasta en el config flow, cada uno con
su propio timeout (20 s aquí, 25 s allá, sin criterio) y su propio manejo del caso «la
respuesta no es JSON» — que ocurre de verdad: el BFF devuelve a veces un cuerpo cuyo nivel
superior es una cadena, y ahí un `.get()` a secas revienta.
"""
from __future__ import annotations

import json
import time

import requests

from . import ebro_auth as A, tsp_sign as S

# Timeouts (segundos). El de escritura es más generoso porque `checkPassword` encadena tres
# llamadas del lado del backend antes de responder.
READ_TIMEOUT_S = 20
WRITE_TIMEOUT_S = 25


def _as_dict(response: requests.Response) -> dict:
    """La respuesta como dict. Nunca lanza.

    El BFF puede devolver un nivel superior que NO es un objeto (una cadena suelta cuando el
    token ha caducado). Devolver `{}` deja que el llamador lo trate como sesión no válida, que
    es lo correcto, en vez de propagar un AttributeError desde dentro de un executor."""
    try:
        parsed = response.json()
    except Exception:
        return {"_raw": response.text[:200]}
    return parsed if isinstance(parsed, dict) else {}


def bff_post(ctx, path: str, body: dict, *, headers: dict | None = None,
             timeout: int = WRITE_TIMEOUT_S) -> dict:
    """POST al BFF (login/PIN/vehículos). Devuelve el JSON como dict.

    Si no se pasan cabeceras se construyen las firmadas del gateway, que es lo que espera
    cualquier ruta del BFF."""
    if headers is None:
        headers = A.headers_post(path, ctx=ctx)
    response = requests.post(ctx.bff + path, data=json.dumps(body), headers=headers,
                             timeout=timeout)
    return _as_dict(response)


def signed_post(ctx, user_token: str, path: str, params: dict,
                *, timeout: int = WRITE_TIMEOUT_S) -> tuple[int, dict]:
    """POST al TSP con el CUERPO firmado. Devuelve (código HTTP, JSON).

    El código HTTP se devuelve aparte porque el backend responde siempre 200 y pone el
    resultado real en el `code` del cuerpo: quien llama necesita ambos para distinguir «el
    servidor no contestó» de «el servidor dijo que no»."""
    ts = int(time.time() * 1000)
    body = S.sign_body(dict(params), ts, half=ctx.sign_key)
    headers = S.auth_headers(user_token, ts)
    headers.update({"Content-Type": "application/json; charset=UTF-8",
                    "Accept": "application/json, text/plain, */*",
                    "User-Agent": "okhttp/4.9.0", "version": A.APP_VERSION,
                    "agent": "android"})
    response = requests.post(ctx.tsp_host + path, data=json.dumps(body), headers=headers,
                             timeout=timeout)
    try:
        return response.status_code, response.json()
    except Exception:
        return response.status_code, {"raw": response.text[:300]}
