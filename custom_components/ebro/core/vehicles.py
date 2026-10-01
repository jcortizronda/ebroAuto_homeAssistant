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
    """Llama a `queryList` con el token actual. Devuelve la respuesta JSON en crudo.

    **El cuerpo NO puede ir vacío.** Con `{}` la nube responde `code=1` y `data: null` — no es un
    error de red ni de sesión, simplemente no contesta la lista. Medido en una instalación real:
    el modelo del vehículo salía vacío y una recarga no dejaba rastro, porque la respuesta llegaba
    «bien» y lo que faltaba era el contenido.

    El config flow lo sabía y mandaba `tUserId` y `channelId`; el coordinator llamaba sin cuerpo y
    se quedaba sin identidad desde siempre. Por eso el cuerpo se construye AQUÍ y no en cada
    llamador: era una de esas diferencias que no se ven hasta que alguien mira el log.

    El config flow sigue pasándolo explícito porque durante el alta el `tUserId` todavía no está
    en el `ctx` — lo acaba de devolver el login."""
    if body is None:
        body = {"channelId": ctx.channel_id}
        if str(getattr(ctx, "tuserid", "") or "").strip():
            body["tUserId"] = str(ctx.tuserid)
    access = wake._access_token(ctx)
    headers = A.headers_post(QUERY_LIST_PATH, ctx=ctx, extra={
        "Authorization": f"Bearer {access}",
        "Content-Type": "application/json; charset=UTF-8",
        "Accept": "application/json, text/plain, */*"})
    return bff_post(ctx, QUERY_LIST_PATH, body, headers=headers)


def _vehicles_with_source(response: dict) -> list[tuple[dict, str]]:
    """Los vehículos de una respuesta de `queryList`, cada uno con la CLAVE de la que salió.

    Formas vistas en vivo: `data` como lista; `data` como dict con una o varias de las listas
    de `_LIST_KEYS`; y `data` como el propio vehículo (un solo coche en la cuenta).

    El recorrido está aquí una sola vez a propósito. `iter_vehicles` y `source_list` necesitan lo
    mismo con distinto detalle, y escribirlo dos veces es exactamente el problema que este módulo
    vino a resolver."""
    data = response.get("data") if isinstance(response, dict) else None
    if isinstance(data, list):
        return [(v, "data") for v in data if isinstance(v, dict)]
    if not isinstance(data, dict):
        return []
    vehicles: list[tuple[dict, str]] = []
    for key in _LIST_KEYS:
        entries = data.get(key)
        if isinstance(entries, list):
            vehicles += [(v, key) for v in entries if isinstance(v, dict)]
    if not vehicles and "vin" in data:
        return [(data, "data")]
    return vehicles


SET_DEFAULT_PATH = "/tsp/v1/app/vmc/setVecDefault"


def set_default(ctx) -> dict:
    """Marca este vehículo como el PREDETERMINADO de la cuenta que ha iniciado sesión.

    No necesita el PIN. Es el segundo paso de la cadena que la app ejecuta antes de cada
    comando —`queryList` → `setVecDefault` → `checkPassword`— y solo el tercero pide el PIN,
    así que esto se puede llamar sin arriesgar un intento del anti-bloqueo.

    **Por qué se llama también al arrancar.** Con una cuenta secundaria la suscripción MQTT se
    concede («Granted QoS 1») y el canal no recibe nada en horas de uso del coche, mientras la
    app oficial con esa misma cuenta sí refleja las aperturas al instante. La ficha del vehículo
    trae un campo `defCar`, y la hipótesis es que la nube solo publica los avisos del vehículo
    marcado como predeterminado en esa cuenta: el canal sería nuestro —de ahí el permiso— pero
    sin nada asociado. La app lo marca al entrar; nosotros solo lo hacíamos antes de un comando
    con PIN, que en esa cuenta nunca llegó a completarse.

    Si la hipótesis falla no se pierde nada: es una petición de solo escritura sobre la
    preferencia de la cuenta, la misma que ya se hace antes de cada comando.
    """
    access = wake._access_token(ctx)
    headers = A.headers_post(SET_DEFAULT_PATH, ctx=ctx, extra={
        "Authorization": f"Bearer {access}",
        "Content-Type": "application/json; charset=UTF-8",
        "Accept": "application/json, text/plain, */*"})
    return bff_post(ctx, SET_DEFAULT_PATH, {"vin": ctx.vin}, headers=headers)


#: Endpoints de SOLO LECTURA sobre los permisos de la cuenta en el vehículo. Salen de comparar
#: los 25 que lleva el binario de la app con los 14 que usamos: estos dos nunca los hemos
#: llamado, y sus nombres son los únicos que hablan de autoridad y de permisos remotos.
#:
#: Importan porque esta nube EXIGE pasos previos — `checkPassword` no devuelve taskId si antes
#: no se ha llamado a `queryList` y `setVecDefault`. Si para una cuenta delegada hiciera falta
#: consultar sus permisos antes de poder ejecutar, estaría aquí. Y aunque no sea un paso previo,
#: su respuesta diría QUÉ puede hacer esta cuenta, que es la pregunta que llevamos toda la noche
#: intentando responder por eliminación.
AUTHORITY_PATHS = (
    "/tsp/v1/app/vmc/queryVehicleAuthority",
    "/tsp/v1/app/vac/queryAuthRemoteList",
    # `vac/query`: el nombre real salio de volver a extraer las cadenas del binario. La primera
    # lectura dio `vac/query6` y `vac/add8` — los digitos eran bytes de la cadena siguiente, no
    # parte de la ruta. Es la consulta del espacio de AUTORIZACIONES, el unico sitio donde
    # podria aparecer QUIEN es el propietario del vehiculo, que es lo que haria falta si el
    # coche publica en el canal del titular y no en el de quien escucha.
    "/tsp/v1/app/vac/query",
)


def _escalar_corto(valor) -> bool:
    """¿Cabe este valor en un resumen? Descarta textos largos y estructuras anidadas."""
    return isinstance(valor, (bool, int, float)) or (isinstance(valor, str) and len(valor) <= 24)


def _resumen_permisos(datos) -> dict:
    """La `permissionList` comprimida para que quepa en el informe.

    Ocupa 34 KB en crudo —medido— asi que volcarla entera no es opcion. Lo que interesa de ella
    es QUE funciones puede ejecutar esta cuenta sobre el coche, y eso vive en los campos cortos
    de cada entrada (un codigo y una bandera); lo que la engorda son descripciones e iconos.

    Se devuelven tres cosas: una entrada COMPLETA para ver la forma —sin ella no se sabe que
    campo mirar—, cuantas hay, y todas proyectadas a sus campos cortos. Es el mismo criterio que
    con los nombres de campo del vehiculo: primero la forma, luego el contenido que importa.
    """
    lista = datos.get("permissionList") if isinstance(datos, dict) else None
    if not isinstance(lista, list) or not lista:
        return {}
    # {id: state} de las 329, ORDENADO. Es lo único que permite comparar dos cuentas de verdad:
    # el resumen anterior recortaba a las 40 primeras, y el backend devuelve la lista en distinto
    # orden en cada llamada, así que se comparaban entradas distintas y parecía que diferían.
    # Ordenado y completo cabe en ~3 KB y se diferencia línea a línea.
    estados = {}
    for entrada in lista:
        if isinstance(entrada, dict):
            estados[str(entrada.get("id"))] = entrada.get("state")
    denegados = sorted((k for k, v in estados.items() if v == 0), key=lambda k: (len(k), k))
    return {
        "perm_count": len(lista),
        # Solo los DENEGADOS, completos y ordenados. Las 329 con su estado no caben en el
        # informe, y comparar dos cuentas se reduce a comparar lo que a cada una le falta.
        #
        # Medido: las dos listas pesan exactamente lo mismo (34863) y SIN EMBARGO difieren —
        # cambiar un `1` por un `0` no cambia el tamaño del texto. Por poco se da por buena la
        # igualdad mirando el peso. Ya hay dos diferencias confirmadas entre titular y delegada
        # («Seguridad» y «Ajustes de seguridad», permitidos solo al titular), asi que esta lista
        # es por CUENTA y no del coche: merece compararse entera.
        # En TROZOS de 50 ids unidos por comas, no como lista. La ocultación del monitor recorta
        # las listas por número de elementos y las cadenas a 400 caracteres, así que los 119
        # denegados se perdían a la mitad justo cuando hacían falta enteros para restar las dos
        # cuentas. Cada trozo cabe de sobra bajo ambos límites.
        **{f"perm_denied_{i // 50 + 1}": ",".join(denegados[i:i + 50])
           for i in range(0, len(denegados), 50)},
        "perm_denied_n": len(denegados),
    }


def claim_authority(ctx) -> dict:
    """`queryVehicleAuthority` como PASO de la cadena del taskId, no como diagnóstico.

    Esta nube exige preparar el terreno: `checkPassword` no devuelve taskId si antes no se ha
    llamado a `queryList` y `setVecDefault` — está comprobado desde el prototipo. La app tiene
    además esta llamada, que nosotros no haciamos nunca, y con una cuenta delegada `checkPassword`
    responde `code=1 'A07908'` con todo lo demas verificado.

    La respuesta confirma que la cuenta SI tiene el permiso del comando que falla («Desbloquear»
    está a 1), asi que no se consulta para decidir nada: se llama por si el backend necesita que
    se haya consultado, igual que con `setVecDefault`."""
    access = wake._access_token(ctx)
    path = AUTHORITY_PATHS[0]
    headers = A.headers_post(path, ctx=ctx, extra={
        "Authorization": f"Bearer {access}",
        "Content-Type": "application/json; charset=UTF-8",
        "Accept": "application/json, text/plain, */*"})
    cuerpo = {"vin": ctx.vin, "channelId": ctx.channel_id}
    if str(getattr(ctx, "tuserid", "") or "").strip():
        cuerpo["tUserId"] = str(ctx.tuserid)
    return bff_post(ctx, path, cuerpo, headers=headers)


GET_TUSERID_PATH = "/tsp/v1/app/auth/getTuserId"


def probe_tuserid(ctx) -> dict:
    """`getTuserId` — ¿el identificador del canal es el de MI cuenta o el del propietario?

    Medido con una cuenta delegada: la suscripción MQTT se concede y NO llega nada, ni siquiera
    la confirmación de un comando que esa misma cuenta acaba de ejecutar con éxito. Que no
    llegue la confirmación del propio comando descarta permisos y contenido: si la nube publica
    y no lo recibimos, el canal al que escuchamos no es el suyo.

    El topic se construye hoy con el `tuserid` de quien inicia sesión. Si para un vehículo
    delegado la nube publica en el canal del PROPIETARIO, este endpoint —que la app tiene y
    nosotros no usábamos— deberia devolver un identificador distinto al nuestro.

    Se compara, NUNCA se publican los identificadores: señalan a la cuenta y están en la lista
    de ocultación del informe."""
    access = wake._access_token(ctx)
    headers = A.headers_post(GET_TUSERID_PATH, ctx=ctx, extra={
        "Authorization": f"Bearer {access}",
        "Content-Type": "application/json; charset=UTF-8",
        "Accept": "application/json, text/plain, */*"})
    cuerpo = {"vin": ctx.vin, "channelId": ctx.channel_id}
    if str(getattr(ctx, "tuserid", "") or "").strip():
        cuerpo["tUserId"] = str(ctx.tuserid)
    j = bff_post(ctx, GET_TUSERID_PATH, cuerpo, headers=headers)
    datos = j.get("data") if isinstance(j, dict) else None
    # El valor puede venir suelto o dentro de un dict; se normaliza a texto para compararlo.
    devuelto = datos
    if isinstance(datos, dict):
        devuelto = datos.get("tUserId") or datos.get("tuserId") or datos.get("userId")
    propio = str(getattr(ctx, "tuserid", "") or "").strip()
    return {
        "code": j.get("code") if isinstance(j, dict) else None,
        "data_type": type(datos).__name__,
        "data_keys": sorted(datos) if isinstance(datos, dict) else None,
        "devuelve_algo": devuelto is not None and str(devuelto).strip() != "",
        # LO QUE IMPORTA, sin exponer ninguno de los dos valores.
        "coincide_con_el_nuestro": (str(devuelto).strip() == propio) if devuelto is not None else None,
    }


def query_authority(ctx) -> dict:
    """Llama a los endpoints de permisos y devuelve {ruta: resumen} para el diagnóstico.

    Resumen, no volcado: el `code` y las claves de primer nivel de `data`, más la propia `data`
    cuando es pequeña — ahí es donde estarían los permisos, que es justo lo que se busca. Pasa
    luego por la ocultación del monitor como todo lo demás.

    Nunca lanza: es un instrumento, y un instrumento roto no puede tumbar el arranque."""
    resumen: dict = {}
    for path in AUTHORITY_PATHS:
        try:
            access = wake._access_token(ctx)
            headers = A.headers_post(path, ctx=ctx, extra={
                "Authorization": f"Bearer {access}",
                "Content-Type": "application/json; charset=UTF-8",
                "Accept": "application/json, text/plain, */*"})
            # `tUserId` incluido: la primera versión mandó solo vin+channelId y las dos rutas
            # contestaron `code=1` con `data: null` — exactamente el sintoma que tenia
            # `queryList` anoche cuando el cuerpo iba incompleto. Es la misma leccion, aplicada
            # al mismo BFF: aqui `code=1` significa «no me gusta la peticion», no «no tienes
            # permiso», asi que no se puede leer como una respuesta sobre la cuenta.
            cuerpo = {"vin": ctx.vin, "channelId": ctx.channel_id}
            if str(getattr(ctx, "tuserid", "") or "").strip():
                cuerpo["tUserId"] = str(ctx.tuserid)
            j = bff_post(ctx, path, cuerpo, headers=headers)
        except Exception as err:
            resumen[path] = {"error": type(err).__name__}
            continue
        datos = j.get("data") if isinstance(j, dict) else None
        resumen[path] = {
            "code": (j or {}).get("code") if isinstance(j, dict) else None,
            "message": str((j or {}).get("message") or "")[:60] if isinstance(j, dict) else None,
            "data_type": type(datos).__name__,
            "data_keys": sorted(datos) if isinstance(datos, dict) else None,
            "size": len(str(datos)) if datos is not None else 0,
            # Las respuestas PEQUEÑAS se guardan enteras. `queryAuthRemoteList` devuelve una
            # lista de 181 bytes que se perdia: el resumen solo sabia tratar el `permissionList`
            # de la otra ruta. Y esa respuesta importa, porque con la cuenta delegada esa misma
            # llamada se rechaza (`code=1`) y con la titular responde — otra diferencia medida
            # entre las dos cuentas.
            "data": datos if datos is not None and len(str(datos)) < 1500 else None,
            **_resumen_permisos(datos),
        }
    return resumen


def iter_vehicles(response: dict) -> list[dict]:
    """Los vehículos de una respuesta de `queryList`, venga en la forma que venga."""
    return [v for v, _src in _vehicles_with_source(response)]


def source_list(response: dict, vin: str) -> dict | None:
    """De QUÉ lista salió este VIN, y con qué tipo de autorización. `None` si no aparece.

    Es un dato de DIAGNÓSTICO, no de funcionamiento: nada del componente cambia según lo que
    devuelva. Existe por una sospecha que no hemos podido confirmar todavía — que una cuenta
    secundaria (invitada) recibe los comandos y las consultas con normalidad pero NO los avisos
    que el coche manda solo (puertas, cierre, maletero), que es el fallo que costó semanas
    diagnosticar en campo. Los nombres de las dos listas apuntan justo ahí:
    `controlCarList` frente a `authorizedControlCarList` («coches que te han autorizado a
    controlar»), y las entradas de la segunda llevan un `authorizeType`.

    No hay prueba: ni la app decompilada ni la captura de tráfico que tenemos mencionan esas
    claves, y no conservamos ninguna respuesta de cuando el usuario estaba en la cuenta
    secundaria. Apuntarlo en el diagnóstico es lo que permitirá confirmarlo o descartarlo con el
    primer informe de alguien afectado, antes de construir un aviso encima de una suposición.
    """
    for vehicle, source in _vehicles_with_source(response):
        if str(vehicle.get("vin")) == vin:
            tipo = vehicle.get("authorizeType")
            return {"list": source, "authorize_type": tipo} if tipo is not None else {"list": source}
    return None


def entry_fields(response: dict, vin: str) -> list[str] | None:
    """Los NOMBRES de los campos que el backend trae para este vehículo. Nunca los valores.

    Instrumento de diagnóstico, no de funcionamiento. Nace de una medida que tumbó la teoría
    que teníamos: con una cuenta secundaria la suscripción MQTT se concede («Granted QoS 1») y
    el canal no recibe nada en horas de uso del coche, pero **la app oficial con esa misma
    cuenta sí se actualiza al instante**. Luego la nube publica; lo que no coincide es el canal.

    El canal se construye hoy con el identificador de QUIEN INICIA SESIÓN. Si el coche publica
    en el del titular, haría falta saber cuál es — y el candidato más barato es esta ficha, que
    de momento se lee entera y se tira salvo cuatro campos. `authorizeType` apareció justo así.

    Solo los nombres: los valores llevan el VIN y el `carToken`, y esto acaba en un informe que
    se comparte."""
    for vehicle, _src in _vehicles_with_source(response):
        if str(vehicle.get("vin")) == vin:
            return sorted(vehicle)
    return None


#: Campos de la ficha del vehículo que se APUNTAN CON SU VALOR en el diagnóstico. Son enums y
#: banderas —no llevan identificadores ni secretos— y cada uno está aquí por un motivo concreto:
#:
#: * `passwordType`  — el principal sospechoso. Ciframos el PIN siempre igual (md5 + SM4 con
#:                     relleno a 32), y si el backend admite más de un esquema este campo diría
#:                     cuál toca. Encajaría con que la app acepte un PIN que a nosotros nos
#:                     rechaza con `checkPassword code=1`.
#: * `authorizeType` — 0 titular / 1 cuenta delegada (medido el 2026-10-01).
#: * `defCar`        — si este coche es el predeterminado de la cuenta; es la otra hipótesis
#:                     abierta sobre por qué no llega la telemetría.
#: * `vehicleType`, `powerType`, `typeCode` — contexto del modelo, por si alguna diferencia
#:                     resulta estar ligada a la variante del coche.
#: * `authorizeStartTime` / `authorizeEndTime` / `authorizeTime` — la delegación tiene VENTANA
#:                     de validez. Se añaden al comprobar que la peticion de `checkPassword` es
#:                     identica a la del prototipo de antes del repositorio: si antes funcionaba
#:                     y ahora el servidor responde «la cuenta no puede ejecutar este comando»,
#:                     lo que cambió no está en nuestro código sino en el permiso.
_FLAG_KEYS = ("passwordType", "authorizeType", "defCar", "vehicleType", "powerType", "typeCode",
              "authorizeStartTime", "authorizeEndTime", "authorizeTime", "isHaveLoAndHi")


def entry_flags(response: dict, vin: str) -> dict | None:
    """Los valores de `_FLAG_KEYS` para este vehículo. Diagnóstico, no funcionamiento.

    A diferencia de `entry_fields` —que da solo los NOMBRES porque los valores llevan el VIN y
    el `carToken`— aquí sí van los valores, pero de una lista blanca cerrada de campos que no
    identifican a nadie. La lista es blanca a propósito: un volcado de la ficha entera acabaría
    exportando el bastidor en el primer informe que alguien comparta."""
    for vehicle, _src in _vehicles_with_source(response):
        if str(vehicle.get("vin")) == vin:
            return {k: vehicle[k] for k in _FLAG_KEYS if k in vehicle}
    return None


def password_type(response: dict, vin: str):
    """El `passwordType` que el backend declara para este vehículo, o `None` si no lo trae.

    Viene como LISTA (`[0]` medido en vivo), lo que se lee como «tipos de verificación
    admitidos»; se toma el primero. Si llegara como escalar se devuelve tal cual.

    Hace falta para `checkPassword`. Tres indicios independientes apuntan ahí: el binario de la
    app oficial contiene `passwordType` entre sus nombres de campo, la nube lo devuelve POR
    CUENTA en la ficha del vehículo, y nosotros no lo mandábamos. Con una cuenta delegada la app
    acepta un PIN que a nosotros nos rechaza con `code=1`, y el resto de diferencias
    —identificador de cuenta, cifrado del PIN, coche predeterminado— ya se descartaron una a una.
    """
    for vehicle, _src in _vehicles_with_source(response):
        if str(vehicle.get("vin")) == vin:
            valor = vehicle.get("passwordType")
            if isinstance(valor, list):
                return valor[0] if valor else None
            return valor
    return None


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


#: Claves bajo las que el backend ha devuelto el MODELO comercial. `fullName` iba primero y era
#: la única: en una instalación real el modelo del dispositivo salía vacío (`model: null` en el
#: registro de dispositivos) porque esta nube no manda esa clave, manda `modelName`. Se prueban
#: las dos por si alguna cuenta devuelve la otra, que es justo el tipo de variación que ya nos
#: ha aparecido con la lista de vehículos.
_MODEL_KEYS = ("modelName", "fullName", "seriesName")


def identity(response: dict, vin: str) -> dict | None:
    """Nombre y modelo del vehículo para el dispositivo de Home Assistant.

    `nickname` es el apodo que el usuario puso en la app; el modelo comercial llega en una de
    `_MODEL_KEYS`. Se prefiere el apodo, y si no hay se usa el modelo. Sin ninguno de los dos
    devuelve `None`, para que el coordinator conserve su valor por defecto en vez de poner un
    nombre vacío."""
    item = find_vehicle(response, vin)
    if not item:
        return None
    nick = str(item.get("nickname") or "").strip()
    model = next((v for v in (str(item.get(k) or "").strip() for k in _MODEL_KEYS) if v), "")
    name = nick or (model.title() if model else "")
    if not name:
        return None
    return {"name": name, "model": model.title() if model else None}
