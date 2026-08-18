"""Conversiones y accesos compartidos por el coordinator y las plataformas.

Todo lo que hay aquí es **puro**: ni Home Assistant, ni red, ni estado. Son las tres o cuatro
operaciones que la integración hace en todas partes sobre los datos que manda el coche, y que
estaban copiadas función a función:

* el coche manda los números como CADENAS (`"0"`, `"0.0"`, `"38.5"`) y a veces `None` o `""`
  cuando el campo no viene → catorce `try: float(v) except (TypeError, ValueError)` repartidos
  por sensor/climate/device_tracker/coordinator, cada uno con su propio criterio sobre qué
  devolver en el caso raro;
* el bloque `realtime` de `coordinator.data` es `None` hasta la primera sonda → once sitios
  escribían `data.get("realtime") or {}`;
* los mensajes de estado que se publican en un sensor tienen un tope de longitud impuesto por
  Home Assistant → seis `[:255]` sueltos, sin nombre que dijera de dónde salía el 255.

Una copia con criterio distinto es un bug esperando: `field_on` ya se escribió dos veces con
comparación textual en un sitio y numérica en otro, y "0.0" salía encendido en la mitad de las
entidades.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .const import MAX_STATUS_LEN

# Valores que el coche usa para decir "este campo no viene", además de `None`.
_ABSENT = ("", "None")


def to_float(value: Any) -> float | None:
    """Número que manda el coche → `float`, o `None` si no es convertible.

    `None` en vez de una excepción o un 0: un campo ausente NO es un cero. Devolver 0 hacía
    que la batería apareciera al 0 % y el odómetro se reiniciara cada vez que el coche mandaba
    un frame incompleto.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_int(value: Any) -> int | None:
    """Igual que `to_float` pero truncando a entero.

    Pasa por `float` a propósito: el coche manda los enteros como `"1.0"` tan a menudo como
    `"1"`, y `int("1.0")` lanza.
    """
    number = to_float(value)
    return None if number is None else int(number)


def is_code(value: Any, code: int) -> bool:
    """True si el campo vale exactamente `code`.

    Los flags del coche son enums numéricos (`chargeState=1` = cargando, `hVoltageState=1` =
    alta tensión encendida). Comparar así evita el `int(float(v)) == 1` envuelto en try/except
    que estaba escrito tres veces en el coordinator.
    """
    return to_float(value) == float(code)


def field_on(value: Any) -> bool | None:
    """Interpreta un campo 5A02 como encendido/abierto (True), apagado/cerrado (False)
    o AUSENTE (None).

    `None` / `"None"` / `""` = campo ausente → devuelve `None`, así a nivel de entidad
    emerge el valor restaurado (o `unknown`) en vez de un falso `False`. En caso contrario
    es verdadero si es distinto de cero, con comparación NUMÉRICA cuando es posible (`"0.0"` =
    apagado, alineado entre binary_sensor/lock/switch/cover); respaldo textual para los booleanos.
    """
    if value is None:
        return None
    text = str(value).strip()
    if text in _ABSENT:
        return None
    number = to_float(text)
    if number is not None:
        return number != 0.0
    return text.lower() not in ("false", "off", "no")


def realtime(data: Mapping[str, Any] | None) -> dict[str, Any]:
    """El bloque `realtime` de `coordinator.data`, siempre un dict.

    Es `None` mientras no haya llegado ninguna sonda (arranque en frío), y ese `None` es la
    razón del `or {}` que estaba repetido once veces.
    """
    if not data:
        return {}
    return data.get("realtime") or {}


def fields(data: Mapping[str, Any] | None) -> dict[str, Any]:
    """El bloque `fields` de `coordinator.data` (telemetría 5A02), siempre un dict."""
    if not data:
        return {}
    return data.get("fields") or {}


def field(data: Mapping[str, Any] | None, key: str) -> Any:
    """El estado de un campo del vehículo, mirando LOS DOS canales.

    El coche informa de las mismas claves por dos vías distintas:

    * los push **5A02 por MQTT**, que solo llegan mientras el coche está despierto;
    * la respuesta de la **sonda realtime**, una foto completa que se puede pedir en cualquier
      momento.

    Estas entidades leían solo la primera. Con un coche que empuja por MQTT no se nota, pero con
    uno que no lo hace —aparcado, o con la app oficial ocupando la única sesión que la nube de
    Chery concede por cuenta— cierre, clima, puertas y maletero se quedan congelados en el
    último valor conocido aunque la sonda esté trayendo el dato correcto en cada lectura.

    **Cuál manda depende de si el coche está despierto**, no de un orden fijo, porque `fields`
    se acumula y NUNCA se vacía:

    * despierto → el push es lo más fresco que hay, y la sonda rellena lo que no venga en él;
    * dormido → lo que queda en `fields` es del último rato que estuvo despierto, así que el
      valor bueno es el de la sonda y el push solo cubre los huecos.

    Sin esa condición, cualquiera de los dos órdenes fijos deja una entidad mintiendo: MQTT
    primero congela al coche dormido, realtime primero congela al coche en marcha entre sondas.
    """
    primary, secondary = (fields(data), realtime(data))
    if not (data or {}).get("awake"):
        primary, secondary = secondary, primary
    value = primary.get(key)
    if value is None:
        value = secondary.get(key)
    return value


def realtime_field(data: Mapping[str, Any] | None, field_name: str) -> str | None:
    """Valor en crudo de un campo `realtime`, o `None` si está ausente/vacío."""
    value = realtime(data).get(field_name)
    if value is None or str(value).strip() in _ABSENT:
        return None
    return str(value)


def truncate_status(message: Any) -> str:
    """Recorta un mensaje al máximo que admite el estado de una entidad de Home Assistant.

    Los mensajes de resultado (comando/despertar/sonda) se publican como ESTADO de un sensor,
    y HA rechaza los estados más largos de `MAX_STATUS_LEN`. Pasarse no da un error visible:
    la entidad simplemente deja de actualizarse.
    """
    return str(message)[:MAX_STATUS_LEN]
