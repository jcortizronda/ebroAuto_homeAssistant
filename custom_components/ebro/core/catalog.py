#!/usr/bin/env python3
"""Catálogo de comandos del coche: qué se le puede pedir y con qué cuerpo.

Datos puros — ni red, ni estado, ni decisiones. Cada entrada dice a qué endpoint va el comando
y qué campos propios lleva; los comunes (`clientType`, `seq`, `taskId`, `vin`, firma) los añade
`commands.send()`.

Estaban dentro de `commands.py`, ocupando 150 de sus 572 líneas y empujando la lógica de envío
hacia el final del archivo. Separarlos deja ver de un vistazo el repertorio del coche, que es
justo lo que se consulta al añadir una entidad nueva.

⚠️ Los cuerpos están reconstruidos 1:1 desde los envelopes reales capturados. Cambiar uno no es
un detalle de estilo: es cambiar lo que se le manda al vehículo.
"""


# ───────────────────────── Catálogo de comandos ─────────────────────────
# Cada entrada: key -> {endpoint, body(fijos específicos), name, icon, group}
# Los campos comunes (clientType/seq/taskId/vin/appId/sign) los añade send().
COMMANDS = [
    # — Clima —
    # clima ON/OFF: temperatura y duración son PARAMÉTRICAS (las pasa la entidad climate vía
    # `params`); los valores del body son solo los por defecto si se invoca sin override.
    ("clima_on",  {"endpoint": "airControl",
                   "body": {"airControlType": "1", "airType": "1", "temperature": "21.0", "times": "15"},
                   "name": "Clima encendido", "icon": "mdi:air-conditioner", "group": "Climatización"}),
    ("clima_off", {"endpoint": "airControl",
                   "body": {"airControlType": "0", "airType": "1", "temperature": "21.0", "times": "15"},
                   "name": "Clima apagado", "icon": "mdi:air-conditioner", "group": "Climatización"}),
    ("defrost_parabrisas", {"endpoint": "frontWindshieldControl",
                   "body": {"frontWindshieldHeat": "1", "times": "15"},
                   "name": "Desempañar parabrisas", "icon": "mdi:car-defrost-front", "group": "Climatización"}),
    ("defrost_parabrisas_off", {"endpoint": "frontWindshieldControl",
                   "body": {"frontWindshieldHeat": "0"},
                   "name": "Desempañar parabrisas OFF", "icon": "mdi:car-defrost-front", "group": "Climatización"}),
    ("defrost_luneta", {"endpoint": "backDefrostingControl",
                   "body": {"backDefrosting": "1", "times": "15"},
                   "name": "Desempañar luneta", "icon": "mdi:car-defrost-rear", "group": "Climatización"}),
    ("defrost_luneta_off", {"endpoint": "backDefrostingControl",
                   "body": {"backDefrosting": "0"},
                   "name": "Desempañar luneta OFF", "icon": "mdi:car-defrost-rear", "group": "Climatización"}),
    ("volante_caliente", {"endpoint": "steeringWheelControl",
                   "body": {"controlType": "1"},
                   "name": "Volante calefactado", "icon": "mdi:steering", "group": "Climatización"}),
    ("volante_caliente_off", {"endpoint": "steeringWheelControl",
                   "body": {"controlType": "0"},
                   "name": "Volante calefactado OFF", "icon": "mdi:steering", "group": "Climatización"}),
    ("asiento_conductor_caliente", {"endpoint": "seatControl",
                   "body": {"mSeatHeating": "3", "times": "15"},
                   "name": "Asiento conductor calefactado", "icon": "mdi:car-seat-heater", "group": "Climatización"}),
    ("asiento_conductor_caliente_off", {"endpoint": "seatControl",
                   "body": {"mSeatHeating": "0"},
                   "name": "Asiento conductor calefactado OFF", "icon": "mdi:car-seat-heater", "group": "Climatización"}),
    ("asiento_conductor_ventilacion", {"endpoint": "seatControl",
                   "body": {"mSeatAiry": "3", "times": "15"},
                   "name": "Asiento conductor ventilado", "icon": "mdi:car-seat-cooler", "group": "Climatización"}),
    ("asiento_conductor_ventilacion_off", {"endpoint": "seatControl",
                   "body": {"mSeatAiry": "0"},
                   "name": "Asiento conductor ventilado OFF", "icon": "mdi:car-seat-cooler", "group": "Climatización"}),
    # Asientos pasajero y traseros — mismo endpoint único `seatControl`, parámetros
    # confirmados por el bean CVSeatControlReqBean (p=pasajero, bl=tras.izq, br=tras.der).
    # Trasero central: el bean NO tiene un parámetro dedicado → sin comando.
    ("asiento_pasajero_caliente", {"endpoint": "seatControl",
                   "body": {"pSeatHeating": "3", "times": "15"},
                   "name": "Asiento pasajero calefactado", "icon": "mdi:car-seat-heater", "group": "Climatización"}),
    ("asiento_pasajero_caliente_off", {"endpoint": "seatControl",
                   "body": {"pSeatHeating": "0"},
                   "name": "Asiento pasajero calefactado OFF", "icon": "mdi:car-seat-heater", "group": "Climatización"}),
    ("asiento_pasajero_ventilacion", {"endpoint": "seatControl",
                   "body": {"pSeatAiry": "3", "times": "15"},
                   "name": "Asiento pasajero ventilado", "icon": "mdi:car-seat-cooler", "group": "Climatización"}),
    ("asiento_pasajero_ventilacion_off", {"endpoint": "seatControl",
                   "body": {"pSeatAiry": "0"},
                   "name": "Asiento pasajero ventilado OFF", "icon": "mdi:car-seat-cooler", "group": "Climatización"}),
    ("asiento_tras_izq_caliente", {"endpoint": "seatControl",
                   "body": {"blSeatHeating": "3", "times": "15"},
                   "name": "Asiento tras. izq. calefactado", "icon": "mdi:car-seat-heater", "group": "Climatización"}),
    ("asiento_tras_izq_caliente_off", {"endpoint": "seatControl",
                   "body": {"blSeatHeating": "0"},
                   "name": "Asiento tras. izq. calefactado OFF", "icon": "mdi:car-seat-heater", "group": "Climatización"}),
    ("asiento_tras_izq_ventilacion", {"endpoint": "seatControl",
                   "body": {"blSeatAiry": "3", "times": "15"},
                   "name": "Asiento tras. izq. ventilado", "icon": "mdi:car-seat-cooler", "group": "Climatización"}),
    ("asiento_tras_izq_ventilacion_off", {"endpoint": "seatControl",
                   "body": {"blSeatAiry": "0"},
                   "name": "Asiento tras. izq. ventilado OFF", "icon": "mdi:car-seat-cooler", "group": "Climatización"}),
    ("asiento_tras_der_caliente", {"endpoint": "seatControl",
                   "body": {"brSeatHeating": "3", "times": "15"},
                   "name": "Asiento tras. der. calefactado", "icon": "mdi:car-seat-heater", "group": "Climatización"}),
    ("asiento_tras_der_caliente_off", {"endpoint": "seatControl",
                   "body": {"brSeatHeating": "0"},
                   "name": "Asiento tras. der. calefactado OFF", "icon": "mdi:car-seat-heater", "group": "Climatización"}),
    ("asiento_tras_der_ventilacion", {"endpoint": "seatControl",
                   "body": {"brSeatAiry": "3", "times": "15"},
                   "name": "Asiento tras. der. ventilado", "icon": "mdi:car-seat-cooler", "group": "Climatización"}),
    ("asiento_tras_der_ventilacion_off", {"endpoint": "seatControl",
                   "body": {"brSeatAiry": "0"},
                   "name": "Asiento tras. der. ventilado OFF", "icon": "mdi:car-seat-cooler", "group": "Climatización"}),

    # NB: los macros clima "todo" (coolingControl/heatingControl: "Enfriar todo"/"Calentar todo")
    # se ELIMINARON — este coche los rechaza con A00084 «comando no permitido» (permiso denegado,
    # verificado en vivo 2026-07-31). La climatización normal (airControl, entidad climate) SÍ va, y
    # cada asiento/desempañador/volante tiene su propio interruptor.

    # — Porte / chiusure —
    ("desbloquear",   {"endpoint": "lockControl", "body": {"lockType": "1"},
                   "name": "Desbloquear puertas", "icon": "mdi:lock-open-variant", "group": "Accesos"}),
    ("bloquear",    {"endpoint": "lockControl", "body": {"lockType": "0"},
                   "name": "Bloquear puertas", "icon": "mdi:lock", "group": "Accesos"}),
    ("maletero_abrir",  {"endpoint": "powerLiftgateControl", "body": {"controlType": "1"},
                   "name": "Abrir maletero", "icon": "mdi:car-back", "group": "Accesos"}),
    ("maletero_cerrar", {"endpoint": "powerLiftgateControl", "body": {"controlType": "0"},
                   "name": "Cerrar maletero", "icon": "mdi:car-back", "group": "Accesos"}),

    # — Ventanillas / techo —
    ("ventanillas_abrir",   {"endpoint": "windowControl", "body": {"controlType": "1"},
                   "name": "Abrir ventanillas", "icon": "mdi:car-door", "group": "Ventanillas y techo"}),
    ("ventanillas_cerrar", {"endpoint": "windowControl", "body": {"controlType": "0"},
                   "name": "Cerrar ventanillas", "icon": "mdi:car-door", "group": "Ventanillas y techo"}),
    ("ventilar_ventanillas", {"endpoint": "windowControl", "body": {"controlType": "2"},
                   "name": "Ventilar ventanillas", "icon": "mdi:weather-windy", "group": "Ventanillas y techo"}),
    ("techo_abrir",   {"endpoint": "skylightControl", "body": {"controlType": "1", "skylightType": "1"},
                   "name": "Abrir techo", "icon": "mdi:car-select", "group": "Ventanillas y techo"}),
    ("techo_cerrar", {"endpoint": "skylightControl", "body": {"controlType": "0", "skylightType": "1"},
                   "name": "Cerrar techo", "icon": "mdi:car-select", "group": "Ventanillas y techo"}),

    # — Carga EV —
    # Carga INMEDIATA inicio/parada (endpoint chargeStartStopControl, bean CVChargeStartStopBean
    # → solo `controlType`; 1=inicia, 0=para, misma convención que todos los *Control).
    ("carga_iniciar", {"endpoint": "chargeStartStopControl", "body": {"controlType": "1"},
                   "name": "Iniciar carga", "icon": "mdi:battery-charging", "group": "Carga"}),
    ("carga_detener", {"endpoint": "chargeStartStopControl", "body": {"controlType": "0"},
                   "name": "Detener carga", "icon": "mdi:battery-off", "group": "Carga"}),
    # Carga PROGRAMADA (chargeAppointControl) — body con ARRAY anidado `chargeAppointPlans` (la
    # firma anidada la resuelve tsp_sign, verificada en 4/4 envelopes reales). mainSwitch =
    # interruptor general; el plan (hora/duración/días) lo pasa la entidad vía `params`.
    # cycleData [1..7] = días; startTime/timeConsuming en MINUTOS; switchStatus = plan activo.
    ("carga_prog_on", {"endpoint": "chargeAppointControl",
                   "body": {"mainSwitch": 1, "chargeAppointPlans": [
                       {"cycleData": [1, 2, 3, 4, 5, 6, 7], "startTime": 480,
                        "switchStatus": 1, "timeConsuming": 360}]},
                   "name": "Carga programada ON", "icon": "mdi:calendar-clock", "group": "Carga"}),
    ("carga_prog_off", {"endpoint": "chargeAppointControl",
                   "body": {"mainSwitch": 0, "chargeAppointPlans": [
                       {"cycleData": [1, 2, 3, 4, 5, 6, 7], "startTime": 480,
                        "switchStatus": 0, "timeConsuming": 360}]},
                   "name": "Carga programada OFF", "icon": "mdi:calendar-remove", "group": "Carga"}),

    # — Otros —
    ("encontrar_coche_luces", {"endpoint": "findCar", "body": {},
                   "name": "Encontrar coche (luces)", "icon": "mdi:car-search", "group": "Otros"}),
    # NB: remoteStart (arranque de motor remoto) ELIMINADO: probado en vivo (2026-06-21) → el
    # coche responde A00084 "No vehicle control command permission" (permiso denegado para este
    # vehículo). Inútil exponer un botón que siempre falla. El bean CVRemoteStartReqBean (sin
    # campos) queda documentado por si en el futuro cambiara el permiso.
    # Petición de posición GPS: NO ejecuta nada; el coche responde con un push MQTT serviceType
    # 1301 (lat/lon) que se conecta al device_tracker. Es el método de la app para la posición en reposo.
    ("localizar_coche_gps", {"endpoint": "vehicleLocation", "body": {},
                   "name": "Localizar coche (GPS)", "icon": "mdi:crosshairs-gps", "group": "Otros"}),

    # — Seguridad — Alarma antirrobo (theftAlarm). Avisos+sirena ante movimiento no autorizado,
    # forzado de puertas, rotura de lunas (descr. oficial de la app). NB: vive en /act (NO
    # /asc/vehicleControl) → usa la clave `path` en vez de `endpoint`. Body = theftAlarmSwitch
    # 0/1; send() añade clientType/seq/vin y el taskId generado (el backend lo exige: A00643
    # sin él). Estado legible vía query_theft_switch() (/act/theftAlarm/querySwitch).
    ("alarma_on",  {"path": "/act/theftAlarm/setSwitch", "body": {"theftAlarmSwitch": "1"},
                   "name": "Alarma activada", "icon": "mdi:shield-car", "group": "Seguridad"}),
    ("alarma_off", {"path": "/act/theftAlarm/setSwitch", "body": {"theftAlarmSwitch": "0"},
                   "name": "Alarma desactivada", "icon": "mdi:shield-off-outline", "group": "Seguridad"}),
]
CMD_MAP = dict(COMMANDS)
