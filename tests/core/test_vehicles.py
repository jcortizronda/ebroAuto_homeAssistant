"""Tests de `core/vehicles.py` — «¿qué coches tiene esta cuenta?».

Este módulo existe porque el parseo era peligroso: el backend devuelve la lista de vehículos
bajo `data` a secas, o bajo una de cuatro claves dentro de `data`, o como el propio coche suelto
si la cuenta solo tiene uno. Ese desenredo estaba escrito tres veces —config flow, coordinator y
`taskid`— y cada copia conocía un subconjunto distinto de las formas. Se unificó para tener una
sola forma de equivocarse... y era el único módulo del componente sin un solo test.

Lo que se prueba es el PARSEO, que es donde está el riesgo: si la nube cambia de forma, el alta
de la integración deja de encontrar el coche. De `query_list` basta comprobar que pega en el
path correcto, porque el HTTP ya está probado en `core/test_http`.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from custom_components.ebro.core import vehicles

COCHE = {"vin": "LSJA0000000000001", "nickname": "Mi Ebro", "modelName": "S900"}
OTRO = {"vin": "LSJA0000000000002", "nickname": "El otro", "modelName": "S800"}


# ───────────────────── iter_vehicles: las seis formas vistas en vivo ─────────────────────


@pytest.mark.parametrize(
    ("forma", "respuesta"),
    [
        ("data como lista", {"data": [COCHE]}),
        ("data.controlCarList", {"data": {"controlCarList": [COCHE]}}),
        ("data.authorizedControlCarList", {"data": {"authorizedControlCarList": [COCHE]}}),
        ("data.carList", {"data": {"carList": [COCHE]}}),
        ("data.vehicles", {"data": {"vehicles": [COCHE]}}),
        ("data como el coche suelto", {"data": COCHE}),
    ],
)
def test_iter_vehicles_entiende_todas_las_formas(forma: str, respuesta: dict) -> None:
    assert vehicles.iter_vehicles(respuesta) == [COCHE], forma


def test_un_coche_propio_y_otro_delegado_llegan_los_dos() -> None:
    """El caso que motivó unificar el módulo: viven en listas DISTINTAS, así que hay que
    acumularlas todas en vez de quedarse con la primera que aparezca."""
    respuesta = {"data": {"controlCarList": [COCHE], "authorizedControlCarList": [OTRO]}}

    assert vehicles.iter_vehicles(respuesta) == [COCHE, OTRO]


@pytest.mark.parametrize(
    "basura",
    [
        {},                                   # sin `data`
        {"data": None},
        {"data": "ups"},                      # `data` no es ni lista ni dict
        {"data": {}},                         # dict vacío, sin `vin`
        {"data": {"controlCarList": "ups"}},  # la clave existe pero no es una lista
        {"data": [None, 42, "x"]},            # entradas que no son dicts
        None,                                 # ni siquiera es un dict
        "respuesta de texto",
    ],
)
def test_iter_vehicles_no_revienta_con_basura(basura) -> None:
    """Es la respuesta de un backend ajeno: una forma inesperada tiene que dar lista vacía, no
    una excepción que tumbe el alta de la integración."""
    assert vehicles.iter_vehicles(basura) == []


def test_data_suelto_solo_cuenta_como_coche_si_trae_vin() -> None:
    """El respaldo «`data` es el propio coche» se apoya en el `vin`: sin él, un dict de
    metadatos cualquiera se colaría como si fuera un vehículo."""
    assert vehicles.iter_vehicles({"data": {"total": 0, "page": 1}}) == []
    assert vehicles.iter_vehicles({"data": {"vin": "X", "total": 0}}) == [{"vin": "X", "total": 0}]


# ───────────────────────────────── vins ─────────────────────────────────


def test_vins_conserva_el_orden_del_backend() -> None:
    assert vehicles.vins({"data": [COCHE, OTRO]}) == [COCHE["vin"], OTRO["vin"]]


def test_vins_descarta_las_entradas_sin_vin() -> None:
    assert vehicles.vins({"data": [{"nickname": "sin vin"}, COCHE]}) == [COCHE["vin"]]


# ────────────────────────────── find_vehicle ──────────────────────────────


def test_find_vehicle_encuentra_el_bastidor_exacto() -> None:
    assert vehicles.find_vehicle({"data": [COCHE, OTRO]}, OTRO["vin"]) is OTRO


def test_find_vehicle_cae_al_primero_si_el_bastidor_no_aparece() -> None:
    """Regla deliberada y documentada: con un solo coche, el backend a veces devuelve el VIN con
    otro formato, y quedarse sin identidad por eso sería peor que usar el único que hay.

    OJO: tal como está escrita, la regla se aplica también con VARIOS coches, y entonces
    devuelve uno equivocado en silencio. Queda fijada aquí para que el día que se cambie sea una
    decisión y no un accidente."""
    assert vehicles.find_vehicle({"data": [COCHE]}, "BASTIDOR-QUE-NO-ESTA") is COCHE
    assert vehicles.find_vehicle({"data": [COCHE, OTRO]}, "BASTIDOR-QUE-NO-ESTA") is COCHE


def test_find_vehicle_sin_coches_devuelve_none() -> None:
    assert vehicles.find_vehicle({"data": []}, COCHE["vin"]) is None


# ──────────────────────────────── identity ────────────────────────────────


def test_identity_prefiere_el_apodo_del_usuario() -> None:
    info = vehicles.identity({"data": [COCHE]}, COCHE["vin"])

    assert info is not None
    assert info["name"] == "Mi Ebro"


def test_identity_usa_el_modelo_cuando_no_hay_apodo() -> None:
    sin_apodo = {"vin": "X", "modelName": "s900"}

    info = vehicles.identity({"data": [sin_apodo]}, "X")

    assert info == {"name": "S900", "model": "S900"}


def test_identity_saca_el_modelo_de_lo_que_manda_el_backend() -> None:
    """El modelo del dispositivo salía VACÍO en una instalación real (`model: null` en el
    registro de dispositivos) porque se buscaba en `fullName`, una clave que esta nube no manda:
    lo que devuelve es `modelName`."""
    info = vehicles.identity({"data": [COCHE]}, COCHE["vin"])

    assert info is not None
    assert info["model"] == "S900"


def test_identity_sin_nombre_ni_modelo_devuelve_none() -> None:
    """`None` y no un dict con cadenas vacías: el coordinator lo usa para conservar su nombre por
    defecto en vez de dejar el dispositivo sin nombre."""
    assert vehicles.identity({"data": [{"vin": "X"}]}, "X") is None


def test_identity_sin_coches_devuelve_none() -> None:
    assert vehicles.identity({"data": []}, "X") is None


# ────────────────────────────── source_list ──────────────────────────────


@pytest.mark.parametrize(
    "clave",
    ["controlCarList", "authorizedControlCarList", "carList", "vehicles"],
)
def test_source_list_dice_de_que_lista_salio_el_coche(clave: str) -> None:
    """Dato de diagnóstico puro: la sospecha es que una cuenta secundaria llega por
    `authorizedControlCarList` y no recibe los avisos que el coche manda solo. Sin apuntarlo, esa
    información se lee y se tira en cada consulta."""
    respuesta = {"data": {clave: [COCHE]}}

    assert vehicles.source_list(respuesta, COCHE["vin"]) == {"list": clave}


def test_source_list_distingue_el_coche_propio_del_delegado() -> None:
    """El caso que da sentido a todo esto: con las dos listas a la vez, cada coche tiene que
    quedar atribuido a la suya."""
    respuesta = {"data": {"controlCarList": [COCHE], "authorizedControlCarList": [OTRO]}}

    assert vehicles.source_list(respuesta, COCHE["vin"]) == {"list": "controlCarList"}
    assert vehicles.source_list(respuesta, OTRO["vin"]) == {"list": "authorizedControlCarList"}


def test_source_list_recoge_el_tipo_de_autorizacion_si_viene() -> None:
    """`authorizeType` solo aparece en las entradas delegadas. Es justo el campo que podría
    decir QUÉ clase de permiso tiene la cuenta, así que viaja con el resto."""
    delegado = {**COCHE, "authorizeType": 0}

    info = vehicles.source_list({"data": {"authorizedControlCarList": [delegado]}}, COCHE["vin"])

    assert info == {"list": "authorizedControlCarList", "authorize_type": 0}


def test_source_list_devuelve_none_si_el_coche_no_aparece() -> None:
    """Sin respaldo al primero, al contrario que `find_vehicle`: aquí una atribución equivocada
    sería peor que no tener el dato, porque es exactamente lo que se quiere medir."""
    assert vehicles.source_list({"data": [COCHE]}, "BASTIDOR-QUE-NO-ESTA") is None
    assert vehicles.source_list({}, COCHE["vin"]) is None


# ────────────────────────────── entry_fields ──────────────────────────────


def test_entry_fields_devuelve_los_nombres_ordenados() -> None:
    """Solo los NOMBRES: los valores llevan el VIN y el `carToken`, y esto acaba en un informe
    de diagnóstico que se comparte con terceros."""
    campos = vehicles.entry_fields({"data": [COCHE]}, COCHE["vin"])

    assert campos == ["modelName", "nickname", "vin"]


def test_entry_fields_no_filtra_ningun_valor() -> None:
    """La red de seguridad del instrumento: si alguna vez devolviera la ficha entera en vez de
    sus claves, estaríamos exportando el bastidor sin darnos cuenta."""
    con_secretos = {**COCHE, "carToken": "token-secreto", "authorizeType": 1}

    campos = vehicles.entry_fields({"data": [con_secretos]}, COCHE["vin"])

    assert campos is not None
    assert "carToken" in campos                      # el nombre sí
    assert not any("token-secreto" in c for c in campos)   # el valor no
    assert not any(COCHE["vin"] in c for c in campos)


def test_entry_fields_sin_el_coche_devuelve_none() -> None:
    assert vehicles.entry_fields({"data": [COCHE]}, "BASTIDOR-QUE-NO-ESTA") is None
    assert vehicles.entry_fields({}, COCHE["vin"]) is None


# ────────────────────────────── entry_flags ──────────────────────────────


def test_entry_flags_recoge_las_banderas_de_la_lista_blanca() -> None:
    ficha = {**COCHE, "passwordType": 1, "authorizeType": 0, "defCar": 1}

    assert vehicles.entry_flags({"data": [ficha]}, COCHE["vin"]) == {
        "passwordType": 1, "authorizeType": 0, "defCar": 1}


def test_entry_flags_no_saca_nada_fuera_de_la_lista_blanca() -> None:
    """La lista es blanca a propósito: un volcado de la ficha entera exportaría el bastidor y el
    `carToken` en el primer informe que alguien comparta."""
    ficha = {**COCHE, "passwordType": 1, "carToken": "token-secreto", "iccid": "8934…"}

    banderas = vehicles.entry_flags({"data": [ficha]}, COCHE["vin"])

    assert banderas == {"passwordType": 1}
    volcado = json.dumps(banderas)
    assert "token-secreto" not in volcado
    assert COCHE["vin"] not in volcado
    assert "8934" not in volcado


def test_entry_flags_omite_las_que_no_vienen() -> None:
    """Sin inventar valores por defecto: que falte una bandera es en sí un dato."""
    assert vehicles.entry_flags({"data": [COCHE]}, COCHE["vin"]) == {}
    assert vehicles.entry_flags({"data": [COCHE]}, "OTRO") is None


# ───────────────────────────── password_type ─────────────────────────────


def test_password_type_toma_el_primero_de_la_lista() -> None:
    """El backend lo devuelve como lista (`[0]` medido en vivo): tipos de verificación
    admitidos. Se manda el primero."""
    assert vehicles.password_type({"data": [{**COCHE, "passwordType": [0]}]}, COCHE["vin"]) == 0
    assert vehicles.password_type({"data": [{**COCHE, "passwordType": [2, 0]}]}, COCHE["vin"]) == 2


def test_password_type_admite_un_escalar() -> None:
    """Por si alguna cuenta lo devuelve suelto: este backend ya nos ha cambiado la forma de una
    respuesta una vez (la lista de vehículos)."""
    assert vehicles.password_type({"data": [{**COCHE, "passwordType": 1}]}, COCHE["vin"]) == 1


def test_password_type_sin_dato_devuelve_none() -> None:
    """`None` tiene que distinguirse de `0`, que es un tipo válido: con `None` el campo NO se
    manda, y mandarlo inventado sería peor que no mandarlo."""
    assert vehicles.password_type({"data": [COCHE]}, COCHE["vin"]) is None
    assert vehicles.password_type({"data": [{**COCHE, "passwordType": []}]}, COCHE["vin"]) is None
    assert vehicles.password_type({"data": [COCHE]}, "OTRO") is None


# ─────────────────────────────── query_list ───────────────────────────────


class _Ctx:
    """Lo mínimo que `query_list` mira del `CoreCtx`."""

    channel_id = "4"
    tuserid = "401643347363401728"


def test_query_list_manda_la_cuenta_y_el_canal() -> None:
    """Con el cuerpo VACÍO la nube responde `code=1` y `data: null` — medido en una instalación
    real, donde el modelo del vehículo llevaba vacío desde siempre por esto. El config flow lo
    mandaba y el coordinator no; construirlo aquí es lo que impide que vuelvan a divergir."""
    with (
        patch.object(vehicles.wake, "_access_token", return_value="AT"),
        patch.object(vehicles.A, "headers_post", return_value={}),
        patch.object(vehicles, "bff_post", return_value={"code": "000000"}) as post,
    ):
        assert vehicles.query_list(_Ctx()) == {"code": "000000"}

    _ctx, path, body = post.call_args[0]
    assert path == vehicles.QUERY_LIST_PATH
    assert body == {"channelId": "4", "tUserId": "401643347363401728"}


def test_query_list_sin_tuserid_manda_solo_el_canal() -> None:
    """Durante el alta el `tUserId` aún no está en el `ctx` —lo acaba de devolver el login—, así
    que el config flow pasa el cuerpo explícito. Mandar `tUserId: ""` sería peor que no mandarlo."""
    ctx = _Ctx()
    ctx.tuserid = ""

    with (
        patch.object(vehicles.wake, "_access_token", return_value="AT"),
        patch.object(vehicles.A, "headers_post", return_value={}),
        patch.object(vehicles, "bff_post", return_value={}) as post,
    ):
        vehicles.query_list(ctx)

    assert post.call_args[0][2] == {"channelId": "4"}


def test_query_list_respeta_el_cuerpo_explicito() -> None:
    """El config flow manda el suyo; no debe pisarse."""
    propio = {"tUserId": "otro", "channelId": "9"}

    with (
        patch.object(vehicles.wake, "_access_token", return_value="AT"),
        patch.object(vehicles.A, "headers_post", return_value={}),
        patch.object(vehicles, "bff_post", return_value={}) as post,
    ):
        vehicles.query_list(_Ctx(), propio)

    assert post.call_args[0][2] == propio
