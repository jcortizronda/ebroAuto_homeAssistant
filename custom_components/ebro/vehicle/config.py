"""El config entry, ya parseado, y la única fábrica del `CoreCtx`.

El componente construye un `CoreCtx` en dos momentos muy distintos: durante el **alta**, cuando
todavía no hay entry y los datos vienen del formulario, y en **runtime**, desde el entry ya
guardado. Eran dos funciones separadas — `config_flow._ctx_del_flow` y
`coordinator._build_ctx` — que rellenaban los mismos doce campos leyendo las mismas claves
`CONF_*` con los mismos respaldos en `DEFAULTS`. Dos fábricas del mismo objeto que podían
divergir sin que nada lo notara: añadir un parámetro de región y olvidarse de una de las dos
significa que el alta funciona y el runtime no (o al revés), con un fallo que solo aparece en
producción.

Aquí hay un solo dataclass con dos constructores y una sola función que fabrica el `CoreCtx`.
Las RUTAS (token, taskId) siguen fuera a propósito: son lo único que de verdad cambia entre los
dos momentos — durante el alta el token va a un archivo «pendiente» que solo al final se mueve
a su nombre definitivo por VIN.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any

from ..const import (
    CONF_BFF,
    CONF_CAR_MQTT_HOST,
    CONF_CAR_MQTT_PORT,
    CONF_CERTS_SRC,
    CONF_CHANNEL_ID,
    CONF_EMAIL,
    CONF_PHONE,
    CONF_PIN,
    CONF_SIGN_KEY,
    CONF_TSP_HOST,
    CONF_TUSERID,
    CONF_VIN,
    DEFAULTS,
)
from ..core import tsp_sign

#: Carpeta de los fuentes de `core/`, que el contexto transporta para los subprocesos de login.
CORE_DIR = os.path.join(os.path.dirname(__file__), "core")

#: Generación automática del taskId. Sin ella los comandos no pueden partir (hacen falta un
#: taskId validado por checkPassword), así que solo se desactiva para diagnóstico. Se lee del
#: entorno UNA vez, aquí: antes estaba enterrada dentro de `coordinator._build_ctx`.
_MINT_TASKID_OFF = ("0", "", "false", "no")


def _mint_taskid_default() -> bool:
    return os.environ.get("EBRO_MINT_TASKID", "1") not in _MINT_TASKID_OFF


@dataclass(frozen=True)
class VehicleConfig:
    """Lo que el componente necesita saber de un vehículo para hablar con él.

    Es solo configuración: ni estado, ni conexiones, ni Home Assistant. El estado por vehículo
    (anti-bloqueo del PIN, caché del taskId, cooldowns) vive en `CoreCtx.state`, que nace con
    el contexto y debe sobrevivir a toda la vida del entry.
    """

    # — identidad de la cuenta y del coche —
    vin: str = ""
    tuserid: str = ""
    pin: str = ""
    email: str = ""
    sign_key: str = ""

    # — región: a qué servidores se habla —
    bff: str = DEFAULTS[CONF_BFF]
    tsp_host: str = DEFAULTS[CONF_TSP_HOST]
    channel_id: str = DEFAULTS[CONF_CHANNEL_ID]
    car_host: str = DEFAULTS[CONF_CAR_MQTT_HOST]
    car_port: int = DEFAULTS[CONF_CAR_MQTT_PORT]

    # — comportamiento —
    certs_src: str = ""
    mint_taskid: bool = True

    @classmethod
    def from_entry(cls, entry) -> VehicleConfig:
        """Desde un config entry ya guardado (runtime)."""
        data = {**DEFAULTS, **dict(entry.data)}
        return cls(
            vin=data[CONF_VIN],
            tuserid=data[CONF_TUSERID],
            pin=data.get(CONF_PIN, ""),
            # El teléfono sustituyó al email como identificador de acceso (2026-07-27); las
            # entradas viejas siguen guardando `email`.
            email=data.get(CONF_EMAIL, "") or data.get(CONF_PHONE, ""),
            # La HALF de firma es una constante de la app: el config flow ya no la pide y solo
            # se respeta el valor guardado si una entrada antigua lo trae.
            sign_key=data.get(CONF_SIGN_KEY, "") or tsp_sign.HALF,
            bff=data[CONF_BFF],
            tsp_host=data[CONF_TSP_HOST],
            channel_id=str(data.get(CONF_CHANNEL_ID, DEFAULTS[CONF_CHANNEL_ID])),
            car_host=data[CONF_CAR_MQTT_HOST],
            car_port=int(data[CONF_CAR_MQTT_PORT]),
            certs_src=data.get(CONF_CERTS_SRC) or "",
            mint_taskid=_mint_taskid_default(),
        )

    @classmethod
    def from_flow_data(cls, data: dict[str, Any]) -> VehicleConfig:
        """Desde los datos que el usuario acaba de teclear en el config flow (alta/reauth).

        Aquí puede faltar casi todo — el VIN no se conoce hasta después del login — así que se
        acepta lo que haya y el resto queda en los valores por defecto de región.
        """
        return cls(
            vin=data.get(CONF_VIN, ""),
            tuserid=data.get(CONF_TUSERID, ""),
            pin=data.get(CONF_PIN, ""),
            email=data.get(CONF_PHONE, ""),
            sign_key=tsp_sign.HALF,
            bff=data.get(CONF_BFF, DEFAULTS[CONF_BFF]),
            tsp_host=data.get(CONF_TSP_HOST, DEFAULTS[CONF_TSP_HOST]),
            channel_id=str(data.get(CONF_CHANNEL_ID, DEFAULTS[CONF_CHANNEL_ID])),
        )


def build_ctx(config: VehicleConfig, *, token_path: str, taskid_file: str = ""):
    """El ÚNICO sitio donde se construye un `CoreCtx`.

    Las rutas van aparte porque son lo único que difiere entre el alta y el runtime: durante el
    alta el token se escribe en un archivo «pendiente» compartido, que solo al crear la entrada
    se mueve a su nombre definitivo por VIN.
    """
    from ..core.context import CoreCtx

    return CoreCtx(
        vin=config.vin,
        tuserid=config.tuserid,
        pin=config.pin,
        email=config.email,
        sign_key=config.sign_key,
        token_path=token_path,
        taskid_file=taskid_file,
        src_dir=CORE_DIR,
        tsp_host=config.tsp_host,
        bff=config.bff,
        channel_id=config.channel_id,
        mint_taskid=config.mint_taskid,
    )
