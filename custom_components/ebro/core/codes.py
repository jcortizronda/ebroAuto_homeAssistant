#!/usr/bin/env python3
"""
codes.py — Mapa ÚNICO de los códigos de respuesta tspconsole/BFF Chery → texto legible.

Fuente de verdad para los ÚNICOS textos de diagnóstico mostrados al usuario (HA/monitor).
El mismo código (p. ej. A07900) tenía antes 3 significados distintos repartidos por
commands/wake/probe/provision; este mapa los unifica. NO cambia la lógica de los comandos:
los módulos deciden el flujo según los códigos, aquí solo está la traducción del código a
una frase.

⚠️ Algunos códigos (sobre todo A07900) son CONTEXTUALES en el backend Chery: el texto
de aquí es el más recurrente/útil; cada llamador puede añadir contexto.
"""

# Código → frase legible (castellano, para no técnicos).
CODE_MEANING = {
    "000000": "ok ✅",
    "A00079": "comando aceptado ✅",
    # A00082: el coche está OCUPADO (procesa un comando cada vez) → el comando NO se ha
    # ejecutado. Transitorio: reintentar en unos segundos (verificado en vivo 2026-06-21).
    "A00082": "coche ocupado ⏳ (hay otro comando en curso) — reinténtalo en unos segundos",
    # A00084 (i18n: "No vehicle control command permission"): la cuenta/vehículo no tiene
    # permiso PARA ESE comando. Visto en vivo con remoteStart (2026-06-21): este Ebro Auto
    # no permite el arranque remoto del motor, mientras clima/cierre/GPS sí funcionan.
    "A00084": "comando no permitido en este coche 🚫 (permiso denegado para esta función)",
    "A00089": "taskId no válido ❌ (hace falta un taskId validado por checkPassword)",
    "A00546": "taskId no válido ❌ (scene incorrecto en checkPassword)",
    "A00567": "parámetros de checkPassword incompletos ❌",
    "A00000": "token caducado/no válido ❌ (vuelve a iniciar sesión)",
    "A07312": "límite de despertar 🚫 (el coche rechaza más despertares ahora, reinténtalo más tarde)",
    # A07900 es contextual: en poll/probe = coche en reposo; con los comandos = firma o
    # car_token no válidos. Texto neutro que cubre el caso más frecuente.
    "A07900": "coche en reposo / no accesible (o firma/car_token no válidos) ⌛",
}


def meaning(code, default=None):
    """Devuelve la frase legible para `code`. Si es desconocido devuelve `default`
    (o una cadena genérica con el código en crudo). Acepta también code=None/no-str."""
    if code is None:
        return default if default is not None else "sin código"
    key = str(code)
    if key in CODE_MEANING:
        return CODE_MEANING[key]
    return default if default is not None else f"código {key}"
