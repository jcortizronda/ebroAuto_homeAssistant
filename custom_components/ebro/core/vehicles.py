#!/usr/bin/env python3
"""Los vehículos de la cuenta: `queryList` y cómo se lee su respuesta.

`/tsp/v1/app/vmc/queryList` es la llamada que responde «qué coches tiene esta cuenta». La
usaban tres sitios y cada uno la escribía entera — login BFF, cabeceras firmadas, POST y
parseo de la respuesta:

* el config flow, para descubrir los VIN al dar de alta;
* el coordinator, para rellenar nombre/modelo del dispositivo;
* `commands._checkpassword`, como primer paso de la generación del taskId.

El parseo era lo peligroso: el backend devuelve la lista bajo `data` a secas, o bajo
`data.controlCarList`, o `data.authorizedControlCarList`, o `data.carList`… y cada copia
conocía un subconjunto distinto de esas formas. Una sola implementación es una sola forma de
equivocarse.
"""
from __future__ import annotations

from . import ebro_auth as A, wake
from .http import bff_post

QUERY_LIST_PATH = "/tsp/v1/app/vmc/queryList"

# Claves bajo las que el backend ha devuelto la lista de vehículos. El orden no importa: se
# acumulan todas, porque un coche propio y uno delegado viven en listas distintas.
_LIST_KEYS = ("controlCarList", "authorizedControlCarList", "carList", "vehicles")


def query_list(ctx, body: dict | None = None) -> dict:
    """Llama a `queryList` con el token actual. Devuelve la respuesta JSON en crudo."""
    access = wake._access_token(ctx)
    headers = A.headers_post(QUERY_LIST_PATH, ctx=ctx, extra={
        "Authorization": f"Bearer {access}",
        "Content-Type": "application/json; charset=UTF-8",
        "Accept": "application/json, text/plain, */*"})
    return bff_post(ctx, QUERY_LIST_PATH, body if body is not None else {}, headers=headers)


def iter_vehicles(response: dict) -> list[dict]:
    """Los vehículos de una respuesta de `queryList`, venga en la forma que venga.

    Formas vistas en vivo: `data` como lista; `data` como dict con una o varias de las listas
    de `_LIST_KEYS`; y `data` como el propio vehículo (un solo coche en la cuenta)."""
    data = response.get("data") if isinstance(response, dict) else None
    if isinstance(data, list):
        return [v for v in data if isinstance(v, dict)]
    if not isinstance(data, dict):
        return []
    vehicles: list[dict] = []
    for key in _LIST_KEYS:
        entries = data.get(key)
        if isinstance(entries, list):
            vehicles += [v for v in entries if isinstance(v, dict)]
    if not vehicles and "vin" in data:
        return [data]
    return vehicles


def vins(response: dict) -> list[str]:
    """Los VIN de la cuenta, en el orden en que los devuelve el backend."""
    return [str(v["vin"]) for v in iter_vehicles(response) if v.get("vin")]


def find_vehicle(response: dict, vin: str) -> dict | None:
    """El vehículo cuyo VIN coincide; si no aparece, el primero de la lista.

    El respaldo al primero es deliberado: con un solo coche en la cuenta, el backend a veces
    devuelve el VIN con distinto formato del que tenemos guardado, y quedarse sin identidad
    del vehículo por eso sería peor que usar el único que hay."""
    vehicles = iter_vehicles(response)
    exact = next((v for v in vehicles if str(v.get("vin")) == vin), None)
    if exact is not None:
        return exact
    return vehicles[0] if vehicles else None


def identity(response: dict, vin: str) -> dict | None:
    """Nombre y modelo del vehículo para el dispositivo de Home Assistant.

    `nickname` es el apodo que el usuario puso en la app; `fullName` es el modelo comercial.
    Se prefiere el apodo, y si no hay se usa el modelo. Sin ninguno de los dos devuelve `None`,
    para que el coordinator conserve su valor por defecto en vez de poner un nombre vacío."""
    item = find_vehicle(response, vin)
    if not item:
        return None
    nick = str(item.get("nickname") or "").strip()
    full = str(item.get("fullName") or "").strip()
    name = nick or (full.title() if full else "")
    if not name:
        return None
    return {"name": name, "model": full.title() if full else None}
