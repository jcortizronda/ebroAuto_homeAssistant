"""Tests de `vehicle_state.py` — el estado en vivo del coche y su lock.

Tres hilos tocan estos datos: el de paho (mensajes MQTT), el executor (sonda y comandos) y el
bucle de eventos de Home Assistant (las entidades leyendo). Antes eran cuatro atributos privados
del coordinator y cada acceso repetía el `with` a mano; olvidarlo una vez da una lectura a medio
escribir, que es el tipo de fallo que no sale en los tests sino de noche y en el coche del
usuario.

Lo que se fija aquí es que **todo lo que sale es una copia** y que el flanco de despertar se
resuelve dentro del mismo lock que la escritura.
"""

from __future__ import annotations

import threading

from custom_components.ebro.vehicle.state import VehicleState

VENTANA = 300.0


def test_los_campos_que_salen_son_una_copia() -> None:
    """Si se devolviera el dict vivo, el hilo de paho podría estar mutándolo mientras Home
    Assistant lo serializa para el informe de diagnóstico."""
    state = VehicleState(VENTANA)
    state.record_message({"doorLock": "0"}, now=1_000.0)

    copia = state.fields()
    copia["doorLock"] = "MANIPULADO"

    assert state.field("doorLock") == "0"


def test_la_posicion_que_sale_es_una_copia() -> None:
    state = VehicleState(VENTANA)
    state.set_position({"lat": "40.4", "lon": "-3.7"})

    copia = state.position
    copia["lat"] = "0"

    assert state.position["lat"] == "40.4"


def test_los_campos_se_acumulan_entre_mensajes() -> None:
    """El coche manda frames parciales: un mensaje nuevo no puede borrar lo ya conocido."""
    state = VehicleState(VENTANA)
    state.record_message({"doorLock": "0", "hood": "0"}, now=1_000.0)

    fields, _ = state.record_message({"hood": "1"}, now=1_001.0)

    assert fields == {"doorLock": "0", "hood": "1"}


def test_merge_position_conserva_los_campos_que_la_sonda_no_trae() -> None:
    """La sonda realtime puede traer solo parte de la geolocalización; perder el resto dejaría
    el device_tracker peor de lo que estaba."""
    state = VehicleState(VENTANA)
    state.set_position({"lat": "40.4", "lon": "-3.7", "altitude": "600"})

    fundida = state.merge_position({"lat": "41.0", "lon": "-3.0"})

    assert fundida == {"lat": "41.0", "lon": "-3.0", "altitude": "600"}


def test_set_position_sustituye_en_vez_de_fundir() -> None:
    """El push 1301 trae la posición completa: fundir dejaría campos de un fix anterior."""
    state = VehicleState(VENTANA)
    state.set_position({"lat": "40.4", "lon": "-3.7", "altitude": "600"})

    nueva = state.set_position({"lat": "41.0", "lon": "-3.0"})

    assert nueva == {"lat": "41.0", "lon": "-3.0"}


def test_despierto_se_mide_por_el_tiempo_desde_el_ultimo_mensaje() -> None:
    """No es un flag: un flag hay que acordarse de apagarlo, y cuando eso se olvidó el botón
    «Despertar coche» respondía «ya está despierto» y no mandaba nada durante días."""
    state = VehicleState(VENTANA)

    assert state.is_awake(now=1_000.0) is False   # nunca ha hablado

    state.record_message({"doorLock": "0"}, now=1_000.0)

    assert state.is_awake(now=1_100.0) is True
    assert state.is_awake(now=1_000.0 + VENTANA + 1) is False


def test_el_flanco_de_despertar_se_resuelve_dentro_del_lock() -> None:
    """`record_message` devuelve si el coche estaba despierto ANTES de este mensaje.

    Leerlo por separado abriría una ventana en la que otro mensaje ya habría movido la marca y
    el flanco — que es lo que dispara la sonda de posición — se perdería."""
    state = VehicleState(VENTANA)

    _, primero = state.record_message({"a": "1"}, now=1_000.0)
    _, segundo = state.record_message({"a": "1"}, now=1_001.0)
    _, tras_dormirse = state.record_message({"a": "1"}, now=1_001.0 + VENTANA + 1)

    assert primero is False        # dormido → es el flanco
    assert segundo is True         # seguía despierto
    assert tras_dormirse is False  # se durmió por el camino → flanco otra vez


def test_escrituras_concurrentes_no_pierden_campos() -> None:
    """Veinte hilos escribiendo a la vez: sin el lock, `dict.update` sobre el mismo dict puede
    perder escrituras. Con él, los 200 campos tienen que estar."""
    state = VehicleState(VENTANA)
    hilos = [
        threading.Thread(
            target=lambda n=n: [
                state.record_message({f"campo_{n}_{i}": str(i)}, now=1_000.0 + i)
                for i in range(10)
            ]
        )
        for n in range(20)
    ]

    for h in hilos:
        h.start()
    for h in hilos:
        h.join()

    assert len(state.fields()) == 200


def test_leer_mientras_se_escribe_no_da_una_lectura_a_medias() -> None:
    """La lectura devuelve una copia coherente, nunca un dict en pleno `update`."""
    state = VehicleState(VENTANA)
    parar = threading.Event()
    incoherencias: list[int] = []

    def escribir() -> None:
        i = 0
        while not parar.is_set():
            state.record_message({f"k{i}": "1", f"k{i}_par": "1"}, now=1_000.0)
            i += 1

    def leer() -> None:
        for _ in range(500):
            snapshot = state.fields()
            # cada escritura mete DOS claves; una copia coherente nunca tiene un número impar
            if len(snapshot) % 2:
                incoherencias.append(len(snapshot))

    escritor = threading.Thread(target=escribir)
    escritor.start()
    leer()
    parar.set()
    escritor.join()

    assert incoherencias == []
