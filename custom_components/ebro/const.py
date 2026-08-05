"""Constantes del componente personalizado Ebro Auto.

Agrupadas por TEMA, no por orden de llegada: el archivo tenía doce grupos temáticos con tres
separadores, así que los intervalos de sondeo, la carga, el MQTT y los tiempos internos estaban
intercalados. Cada sección lleva debajo el porqué de los valores que no son obvios — casi todos
salen de una comprobación en campo con el coche delante, no de una preferencia.
"""

DOMAIN = "ebro"
PLATFORMS = ["sensor", "binary_sensor", "button", "lock", "switch", "climate",
             "number", "time", "cover", "device_tracker"]

# Longitud máxima del ESTADO de una entidad en Home Assistant. Los mensajes de resultado
# (comando/despertar/sonda) se publican como estado de un sensor: pasarse de aquí no da un
# error visible, la entidad simplemente deja de actualizarse. Ver `helpers.truncate_status`.
MAX_STATUS_LEN = 255


# ─────────────────── Cuenta y vehículo · claves del config entry ───────────────────
CONF_EMAIL = "email"
CONF_PIN = "pin"
CONF_VIN = "vin"
CONF_TUSERID = "tuserid"
CONF_SIGN_KEY = "sign_key"
# Login Ebro = teléfono + contraseña (VERIFICADO 2026-07-27). El email/OTP antiguo no se usa.
CONF_PHONE = "phone"
CONF_PASSWORD = "password"
CONF_AREA_CODE = "area_code"
DEFAULT_AREA_CODE = "34"     # España. Ej. Italia=39, Francia=33, Alemania=49.

# Identidad del vehículo para el dispositivo HA (nombre dinámico: "Ebro S700", "Ebro S800"…).
# `vehicle_name` = apodo/modelo de la app, guardado en entry.data (capturado en el config flow o
# rellenado después); es también una OPCIÓN para el override manual. model/brand solo en entry.data.
CONF_VEHICLE_NAME = "vehicle_name"
DATA_VEHICLE_MODEL = "vehicle_model"
DATA_VEHICLE_BRAND = "vehicle_brand"
# valor por defecto cuando el modelo aún no se conoce
DEFAULT_VEHICLE_NAME = "Ebro Auto"
# Marca del vehículo: constante, esta integración es solo para Ebro.
VEHICLE_BRAND = "Ebro"


# ─────────────────────── Región · a qué servidores se habla ───────────────────────
# Por defecto Europa. Expuestos como opciones para soportar otras regiones.
CONF_BFF = "bff"
CONF_TSP_HOST = "tsp_host"
CONF_CAR_MQTT_HOST = "car_mqtt_host"
CONF_CAR_MQTT_PORT = "car_mqtt_port"
CONF_CHANNEL_ID = "channel_id"

DEFAULTS = {
    # BFF (login/auth/vmc) = legend.ebroauto.com; telemetría/control = tspconsole (VERIFICADO).
    CONF_BFF: "https://legend.ebroauto.com/api",
    CONF_TSP_HOST: "https://tspconsole-eu.ebroauto.com",
    CONF_CAR_MQTT_HOST: "tspemqx-app-eu.ebroauto.com",
    CONF_CAR_MQTT_PORT: 8083,
    CONF_CHANNEL_ID: "4",
}


# ────────────────────────── Certificados mutual-TLS MQTT ──────────────────────────
# Carpeta (dentro del sistema de archivos de HA) desde la que importar los 4 certificados a la
# certs_dir por entrada. Vacío = los certificados se ponen a mano.
CONF_CERTS_SRC = "certs_src"

# Los 4 archivos esperados en la certs_dir por entrada (= los del puente certs_eu/).
CERT_FILES = ("ca.pem", "client.pem", "client.key", "eu_prd_cheryinternational.cer")

# Permisos en disco: son credenciales, solo su propietario.
CERT_DIR_MODE = 0o700
CERT_FILE_MODE = 0o600


# ───────────────────────────── Canal MQTT del coche ─────────────────────────────
# Constante de app compartida (no un secreto del usuario): semilla para derivar la contraseña.
CAR_SEED = "fa89db3abe8045919d70c6ed3cc65bc5"

# `serviceType` de los mensajes que publica el coche. Se discrimina por el TIPO, no por los
# campos presentes: un 5A02 y un 1301 pueden traer las mismas claves y significan cosas
# distintas (telemetría de estado vs. reporte de posición).
SERVICE_TYPE_TELEMETRY = "5A02"   # estado del vehículo (puertas, clima, cierre, cable…)
SERVICE_TYPE_POSITION = "1301"    # reporte de posición GPS → device_tracker

# Latido MQTT y reconexión. El backoff NO es el de paho por defecto (1 s fijo): con la red
# caída eso es un intento por segundo indefinidamente, justo el patrón que los gateways
# sancionan.
MQTT_KEEPALIVE_S = 60
MQTT_RECONNECT_MIN_S = 1
MQTT_RECONNECT_MAX_S = 120

# Cuánto tiempo sin recibir nada se sigue considerando al coche «despierto».
DEFAULT_AWAKE_WINDOW = 300

# Códigos `result` de los push de CONFIRMACIÓN de comando (110x/1105/1135…). Interpretación
# conservadora, basada en los envelopes reales capturados: ver `telemetry.format_command_result`.
CMD_RESULT_ASYNC_RUNNING = "5"       # operación asíncrona aún en curso (siempre con hasAsy=1)
CMD_RESULT_APPLIED = ("1", "2")      # ejecutado / aplicado (estado del vehículo actualizado)


# ─────────────── Sondeo de telemetría (canal REALTIME) por ESTADO ───────────────
# El canal MQTT (puertas, cierre, cable, motor…) llega solo y gratis. El canal REALTIME
# (batería, autonomía, alta tensión, progreso de carga…) NO se empuja: hay que sondear.
# El sondeo automático es de SOLO LECTURA (nunca despierta el coche); se dispara por eventos
# MQTT gratis (enchufar; y la RÁFAGA de "Último contacto" que el coche emite al circular) y elige
# el ritmo según el ESTADO del coche. Todos los intervalos en MINUTOS, configurables en Opciones
# (0 = desactivado en ese estado). Prioridad de estado (de mayor a menor) y su intervalo:
#   1. Cargando           (chargeState=cargando, realtime)             → CONF_POLL_CHARGING   (15 min)
#   2. Enchufado          (chargeGunState on, MQTT)                    → CONF_POLL_PLUGGED    (30 min)
#   3. En marcha          (alta tensión on + RÁFAGA de MQTT)          → CONF_POLL_MOVING     (3 min)
#   4. En marcha detenido (alta tensión on SIN ráfaga: semáforo, etc.) → CONF_POLL_MOVING_IDLE (5 min)
#   5. Parado             (resto; alta tensión apagada)                → CONF_POLL_PARKED     (0 = off)
CONF_POLL_PARKED = "poll_normal_min"      # (clave heredada) parado; 0 = no tocar el coche
CONF_POLL_CHARGING = "poll_charging_min"  # cargando de verdad
CONF_POLL_PLUGGED = "poll_plugged_min"    # enchufado, esperando (p. ej. carga programada)
CONF_POLL_MOVING = "poll_moving_min"      # en marcha (ráfaga de MQTT + alta tensión)
CONF_POLL_MOVING_IDLE = "poll_moving_idle_min"  # alta tensión encendida pero detenido (sin ráfaga)
DEFAULT_POLL_PARKED_MIN = 0
DEFAULT_POLL_CHARGING_MIN = 15
DEFAULT_POLL_PLUGGED_MIN = 30
DEFAULT_POLL_MOVING_MIN = 3
DEFAULT_POLL_MOVING_IDLE_MIN = 5

# Detección de MARCHA por RÁFAGA de MQTT: al circular, el coche emite "Último contacto" seguido.
# ≥ MOVE_BURST_COUNT mensajes en los últimos MOVE_BURST_WINDOW_S segundos = en movimiento. Se
# mantiene la marcha mientras la alta tensión siga encendida; sin ráfaga (parado con AT) baja al
# intervalo "en marcha detenido"; al apagarse la alta tensión, se para del todo.
MOVE_BURST_COUNT = 5
MOVE_BURST_WINDOW_S = 30

# Tope de seguridad (MINUTOS, configurable en Opciones; 0 = sin límite) de "enchufado sin cargar":
# no sondear indefinidamente un coche enchufado que nunca carga. 0 por defecto: la carga programada
# puede arrancar horas después de enchufar y un tope corto la haría perderse.
CONF_PLUGGED_WAIT_MAX = "plugged_wait_max_min"
DEFAULT_PLUGGED_WAIT_MAX_MIN = 0


# ──────────────────── Climatización y carga programada ────────────────────
# Duración `times` del comando airControl (la fija el number «Duración de la climatización»).
DEFAULT_CLIMA_DURATION_MIN = 15

# Hora de inicio y duración por defecto de la carga programada (minutos desde medianoche y
# minutos de duración). Los usan la entidad `time` que los deja fijar y el coordinator, que
# necesita un valor válido incluso antes de que esa entidad se haya añadido.
DEFAULT_CHARGE_START_MIN = 8 * 60      # 08:00
DEFAULT_CHARGE_DURATION_MIN = 6 * 60   # 6 h

# El coche exige una duración MÍNIMA de 1 h en el plan de carga (una menor da code 89).
CHARGE_MIN_DURATION_MIN = 60      # duración mínima que acepta el coche
CHARGE_STOP_DURATION_MIN = 60     # duración de la ventana del "stop" (mínimo del coche)
CHARGE_STOP_START_BACK_MIN = 90   # startTime = ahora − 1:30 → ventana [ahora−90, ahora−30], ya terminada

# Límite de carga por SOFTWARE: parar la carga al llegar a un %, ya que el coche no tiene tope de
# SOC nativo (chargeStartStopControl da A00084). Al alcanzar el objetivo se impone una programación
# con la ventana YA PASADA (única forma de parar en este coche: una programación fuera del horario
# actual). Se controla con el interruptor "Limitar carga al %" + el number "Límite de carga (%)".
DEFAULT_CHARGE_LIMIT_SOC = 80
CHARGE_LIMIT_NEAR_SOC = 5          # a menos de estos % del objetivo se sondea más fino
CHARGE_LIMIT_NEAR_MIN = 5         # intervalo (min) del sondeo fino cerca del límite

# Minutos de un día: los horarios de carga viajan como minutos-desde-medianoche.
MINUTES_PER_DAY = 24 * 60
SECONDS_PER_DAY = 24 * 60 * 60


# ─────────────────────────────── Cola de comandos ───────────────────────────────
# El coche ejecuta UN comando cada vez (A00082 = "vehículo ocupado"), así que los comandos se
# serializan. Un segundo comando (o un doble toque) ESPERA su turno en vez de ser rechazado.
# Tras un envío se deja respirar al coche hasta su confirmación MQTT o como mucho
# COMMAND_SETTLE_S, para que el siguiente de la cola no arranque mientras aún está ocupado.
# COMMAND_QUEUE_WAIT limita la espera en cola: pasado ese tiempo, el comando falla con un mensaje claro.
COMMAND_SETTLE_S = 5
COMMAND_QUEUE_WAIT = 30
# Paso con el que se comprueba si el coche ya confirmó el comando anterior.
COMMAND_SETTLE_STEP_S = 0.5


# ─────────────────────── Tiempos del ciclo de vida interno ───────────────────────
# Keep-alive de la sesión: cada cuánto se revalida el token para que no caduque en reposo.
DEFAULT_SESSION_EVERY = 900

# Sonda semilla tras el arranque de HA: da tiempo a que MQTT conecte antes de leer, para que
# la primera lectura ya clasifique bien el estado (cargando/marcha/parado).
SEED_PROBE_DELAY_S = 15

# «Actualizar estado completo»: espera entre lecturas realtime mientras se aguarda a que la
# alta tensión suba, y cuántas veces reintentarlo. HV_WAIT_ATTEMPTS × POLL_WAKE_WAIT ≈ 2,5 min.
# Solo lo usa ese botón manual: el sondeo automático nunca despierta el coche.
POLL_WAKE_WAIT = 25
HV_WAIT_ATTEMPTS = 6


# ──────────────────── Reparto de entidades entre plataformas ────────────────────
# Campos del coche (5A02) representados por entidades nativas ACCIONABLES (lock/switch/cover):
# se excluyen de la creación de sensor/binary_sensor "de solo lectura" para no duplicarlos.
# Los campos de confort (desempañadores/volante/asientos conductor-pasajero-traseros) son
# interruptores ON/OFF (ver switch.py). NB: el asiento trasero CENTRAL
# (mSeatHeatingState2/mSeatVentilateState2) NO tiene un comando dedicado → queda de solo lectura.
FIELDS_AS_RICH_ENTITY = {
    "doorLock", "frontHVACState", "trunkDoor", "sunroofState",
    "frontWindshieldHeat", "rWinHeatingState", "steerWheelHeating",
    "dSeatHeatingState", "dSeatVentilateState",
    # asiento del pasajero
    "pSeatHeatingState", "pSeatVentilateState",
    # asientos traseros IZQ/DER (telemetría *State2 ↔ comando bl/br SeatControl)
    "lSeatHeatingState2", "lSeatVentilateState2",
    "rSeatHeatingState2", "rSeatVentilateState2",
}

# Comandos del catálogo gestionados por lock/switch/cover → excluidos de los botones sueltos
# (pulsar el lock/switch/cover invoca el mismo comando del catálogo).
COMMANDS_AS_RICH_ENTITY = {
    "bloquear", "desbloquear",
    # clima_on/clima_off pilotados por la entidad climate (climate.py) → sin botones.
    "clima_on", "clima_off",
    # carga EV: interruptores dedicados (switch.py) → sin botones sueltos.
    "carga_iniciar", "carga_detener", "carga_prog_on", "carga_prog_off",
    # NB: los macros clima "todo" (clima_enfriar_*/clima_calentar_*) se eliminaron: A00084 en este coche.
    # alarma antirrobo: interruptor dedicado (EbroTheftAlarmSwitch) → sin botones on/off separados.
    "alarma_on", "alarma_off",
    "maletero_abrir", "maletero_cerrar",
    "ventanillas_abrir", "ventanillas_cerrar",
    "techo_abrir", "techo_cerrar",
    # confort: cada función es un interruptor (ON+OFF) → sin botones sueltos
    "defrost_parabrisas", "defrost_parabrisas_off",
    "defrost_luneta", "defrost_luneta_off",
    "volante_caliente", "volante_caliente_off",
    "asiento_conductor_caliente", "asiento_conductor_caliente_off",
    "asiento_conductor_ventilacion", "asiento_conductor_ventilacion_off",
    "asiento_pasajero_caliente", "asiento_pasajero_caliente_off",
    "asiento_pasajero_ventilacion", "asiento_pasajero_ventilacion_off",
    "asiento_tras_izq_caliente", "asiento_tras_izq_caliente_off",
    "asiento_tras_izq_ventilacion", "asiento_tras_izq_ventilacion_off",
    "asiento_tras_der_caliente", "asiento_tras_der_caliente_off",
    "asiento_tras_der_ventilacion", "asiento_tras_der_ventilacion_off",
}


# ─────────────────── Monitor de diagnóstico (desarrollador) ───────────────────
# Ver vehicle/diag.py. No es una función de usuario: no tiene interruptor en la interfaz. Se
# activa creando el archivo bandera de abajo en la carpeta de configuración de HA (contenido =
# días de duración) y se apaga solo al vencer. Sin el archivo el código está dormido: el
# grabador queda en None → coste nulo.
DIAG_SWITCH_FILE = "ebro_diag.on"
# Suelo del autoapagado: aunque la bandera ya haya vencido, se le da este margen para no
# apagarlo en el mismo arranque que lo enciende.
DIAG_MIN_AUTOSTOP_S = 60.0
