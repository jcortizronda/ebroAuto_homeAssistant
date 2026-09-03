# Arquitectura

Notas para trabajar en el código. Para instalar y usar la integración, ver `README.md`.

La integración habla con el coche por dos canales distintos, y de esa diferencia sale casi todo
el diseño:

- **MQTT** (mutual-TLS, `tspemqx-app-eu`): el coche empuja puertas, cierre, cable, motor. Llega
  solo y no cuesta nada.
- **REST** (`tspconsole-eu`): batería, autonomía, alta tensión, progreso de carga y los
  comandos. Hay que pedirlo. Pedirlo de más consume la batería de 12 V y desconecta la app
  oficial, porque la nube de Chery solo admite una sesión por cuenta.

## Estructura

```
custom_components/ebro/
├── __init__.py, config_flow.py, diagnostics.py, repairs.py
├── sensor.py, binary_sensor.py, switch.py, button.py, climate.py,
│   cover.py, lock.py, number.py, time.py, device_tracker.py
│       ↑ Home Assistant los descubre por nombre y ruta. No se pueden mover.
│
├── const.py, helpers.py, models.py, entity.py
│       ↑ lo que importa todo el mundo
│
├── vehicle/   el coche: estado, conexión y decisiones. Conoce Home Assistant.
└── core/      el protocolo Chery. No importa nada de Home Assistant.
```

Los imports van en un sentido:
`const/helpers/models` ← `core/` ← `vehicle/` ← `entity` ← plataformas.

`core/` está aislado de Home Assistant porque es la parte deducida de la app oficial y la que
más cuesta reconstruir. Al no depender de `hass`, la firma de un comando o la clasificación de
un código de error se prueban en milisegundos.

## Cómo llega un dato del coche a la pantalla

```
coche ──MQTT──▶ vehicle/mqtt_client.py    conecta, suscribe, entrega bytes
                        │                 (hilo de paho, no el de HA)
                        ▼
                vehicle/telemetry.py      parse_car_message(): tipo de mensaje,
                        │                 qué campos son estado, si trae datos o
                        │                 es el latido que emite al circular
                        ▼
                vehicle/coordinator.py    _on_car_message
                        ├──▶ vehicle/state.py      guarda campos y posición
                        ├──▶ vehicle/polling.py    ¿acelera el sondeo?
                        ▼
                coordinator.data          lo leen todas las entidades
```

## Cómo llega un comando al coche

```
usuario ──▶ switch/button/lock…     _run_command(): muestra ya el estado objetivo
                   │                (optimista), la UI no se queda quieta
                   ▼
        coordinator.async_send_command    cola: el coche ejecuta uno cada vez
                   │
                   ▼  en executor, nunca en el bucle de eventos
        core/commands.send ──▶ core/taskid.py    ¿taskId en caché? si no,
                   │                             checkPassword con el PIN
                   ▼
        core/http.signed_post ──▶ coche
                   │
                   ▼  el backend siempre responde HTTP 200; el resultado va en `code`
        core/routing.py     ¿éxito? ¿qué remedio? ¿cuenta para el anti-bloqueo?
                   │        ¿hay que rehacer el taskId?
                   ▼
        vehicle/session_manager.route_remedy
                   ├─ PIN erróneo   → Repair de reconfiguración
                   ├─ sesión muerta → reautenticación de HA
                   └─ resto         → aviso
```

El coche confirma aparte, por MQTT, con un push `110x` que entra por el recorrido anterior. Por
eso una entidad optimista se corrige sola cuando el coche no pudo ejecutar.

## Los módulos

### Nivel superior

| Archivo | |
|---|---|
| `__init__.py` | Arranque y parada: certificados, sesión, MQTT, timers, plataformas. |
| `config_flow.py` | Alta (teléfono + contraseña → descubrir VIN), reautenticación, opciones. |
| `diagnostics.py` | «Descargar diagnóstico», con lo sensible ya ocultado. |
| `repairs.py` | Arregla el aviso de PIN erróneo sin desmontar la integración. |
| `sensor.py`, `binary_sensor.py` | Lecturas del 5A02 y del canal realtime, desde tablas de specs. |
| `switch.py`, `climate.py`, `lock.py`, `cover.py`, `button.py` | Actuadores: estado + comandos en una entidad. |
| `number.py`, `time.py` | Preferencias locales. No mandan nada al coche. |
| `device_tracker.py` | Posición en el mapa. |
| `const.py` | Constantes por secciones, con la comprobación en campo que justifica cada valor. |
| `helpers.py` | `to_float`, `field_on`, `field`, `realtime`. Funciones puras. |
| `models.py` | `EbroConfigEntry`, `ChargePreferences`, validación del destino de una preferencia. |
| `entity.py` | Entidad base + mixins de restauración de estado y de estado optimista. |

### `vehicle/`

| Archivo | |
|---|---|
| `coordinator.py` | Fachada: estado, cola de comandos, acciones. Delega en el resto. |
| `state.py` | Telemetría, posición y «¿despierto?» tras un lock. Devuelve copias. |
| `mqtt_client.py` | Conexión mutual-TLS con paho. |
| `telemetry.py` | Mapa de campos del coche y parseo de mensajes. Puro. |
| `poll_policy.py` | Dadas unas condiciones, qué estado y cada cuántos minutos. Puro. |
| `polling.py` | Lee condiciones, programa y reprograma el bucle de sondeo. |
| `charging.py` | Carga programada y límite de batería por software. |
| `session_manager.py` | Keep-alive, reautenticación, aviso persistente, Repair del PIN. |
| `certificates.py` | De dónde salen los certificados mutual-TLS. |
| `cert_bundle.py` | Desofusca el bundle por región (`certs/store.json`). |
| `config.py` | El config entry parseado. Única fábrica del `CoreCtx`. |
| `timers.py` | Registro de timers. Tras `close()` ninguno puede rearmarse. |
| `diag.py`, `monitor.py` | Monitor de diagnóstico y su ciclo de vida. |

### `core/`

| Archivo | |
|---|---|
| `context.py` | `CoreCtx`: configuración y estado de un vehículo, pasado por argumento. |
| `catalog.py` | Repertorio de comandos: endpoint y cuerpo de cada uno. |
| `commands.py` | Envío, con un reintento si el taskId caducó. |
| `taskid.py` | Cómo se consigue un taskId. Ver la nota sobre el PIN más abajo. |
| `pin_lockout.py` | Anti-bloqueo del PIN. Solo se entra por `attempt()`. |
| `routing.py` | Tabla de códigos del backend → éxito, remedio, bloqueo, reintento. |
| `errors.py`, `codes.py` | El error de comando; los códigos como frases legibles. |
| `http.py` | Las dos formas de hablar con el backend (BFF y TSP) y sus timeouts. |
| `wake.py` | Despertar por SMS y esperar. Tiene rate-limit real. |
| `probe.py` | Una lectura del canal realtime. Nunca despierta el coche. |
| `session.py` | ¿El token vive? Distingue revocado de sin red. |
| `vehicles.py` | `queryList` y su parseo (llega bajo cuatro claves distintas). |
| `ebro_login.py`, `ebro_auth.py`, `tsp_sign.py` | Login OAuth, cabeceras firmadas, firma del cuerpo. |

## Cosas que conviene saber antes de tocar

**El estado del coche está tras un lock, y no se puede sortear.** Lo tocan tres hilos: paho,
los executors y el bucle de eventos. `VehicleState` no expone sus dicts, devuelve copias.
`record_message()` resuelve los campos y el flanco de despertar en la misma operación, porque
leerlos por separado deja una ventana en la que el flanco se pierde.

**Los dos canales traen las mismas claves, y cuál manda depende del coche.** `doorLock`,
`trunkDoor`, `frontHVACState` y las puertas vienen tanto en el push 5A02 como en la sonda
realtime. `helpers.field()` prefiere el push mientras el coche está despierto y la sonda cuando
está dormido: `fields` se acumula y nunca se vacía, así que con el coche parado lo que queda ahí
es historia. Cualquiera de los dos órdenes fijos deja una entidad mintiendo — leer solo el push
congelaba el cierre y el maletero en cuanto MQTT se quedaba seco, que es lo que pasa con la app
oficial abierta.

**El estado optimista caduca con la verdad, venga por donde venga.** Tras un comando la
entidad muestra el objetivo, porque el coche tarda en confirmar. Ese objetivo cede en cuanto
llega un push MQTT (`last_seen`) **o** una sonda con contenido distinto (`car_data_ts`).
Anclarlo solo a MQTT dejaba el objetivo clavado para siempre en un coche que no empuja.

**El canal MQTT solo entrega en la cuenta propietaria del vehículo.** El topic va contra el id de
usuario (`app/<canal>/<tuserid>/account/msgCenter/msg`). Con una cuenta invitada el broker acepta
la conexión y **concede la suscripción** —`car_subscribed: true`, «Granted QoS 1»— pero ahí no se
publica nada. Comprobado en las dos direcciones sobre la misma instalación. El porqué no lo sabemos:
el REST sí responde igual para las dos cuentas, así que no es una falta de permisos sobre el
vehículo. Por eso el diagnóstico distingue conectado de suscrito: sin esa distinción, la única
pista era un `fields_count: 0` que también significa «el coche está dormido».

Queda un cabo suelto: **la app oficial, con esa misma cuenta secundaria, refleja las aperturas al
instante**. Medido: instantáneo, o sea push — hay algo publicándose en un topic que no conocemos,
porque solo tenemos el que se dedujo del APK. Para averiguar cuál, con el monitor de diagnóstico
encendido la suscripción pasa del topic exacto al comodín `app/<canal>/<tuserid>/#`, y todo lo que
llegue por un topic distinto del conocido se APUNTA sin tocar el estado. Si la ACL deniega el
comodín, se vuelve solo al topic exacto: pedir de más no puede dejar la integración sin escuchar.

**`queryVehicleLocation` LEE la última posición; `vehicleLocation` la PIDE.** Los nombres lo
dicen y la diferencia se nota: la sonda usa la consulta —devuelve lo último que sabe la nube,
sin tocar el coche— mientras que el botón «Localizar coche (GPS)» manda el comando con taskId y
hace que el coche reporte un fix nuevo. Es deliberado: la sonda existe para no despertar al
coche. Y la posición sale ÚNICAMENTE de ahí: la respuesta de `/asr/manager/realtime` no trae
coordenadas (verificado sobre una captura real de la app: 80 campos, ninguna). Lo que mantiene
vivo el mapa en el uso normal es el push MQTT de posición, gratis y automático — sin ese canal,
la única forma de mover el punto es el comando.

**La sonda responde igual de bien con el coche dormido, y no significa lo mismo.** Despierto
contesta el coche; dormido, la nube devuelve la última instantánea que guardó, que puede tener
media hora. `onlineStatus` de la propia respuesta es lo que los distingue, y `probe.freshness()`
lo dice en el texto del sensor en vez de anunciar las dos cosas como tiempo real.

**Este coche NO empuja posición por MQTT.** Comprobado sobre una semana de registro con la
cuenta propietaria: cientos de mensajes, todos `5A02` (estado) y dos `110D` (confirmación de
comando). Ni un solo `1301`. La posición depende por completo de la sonda, y `queryVehicleLocation`
es una consulta a la nube, no una orden al coche — de ahí que el mapa solo salte de verdad con
«Localizar coche (GPS)».

**La programación de carga se lee del coche y se adopta CUANDO CAMBIA.** Las entidades de hora
y duración eran solo la preferencia local —lo que se envía al pulsar—, y una programación puesta
desde la app oficial o desde el propio coche no las tocaba. Ahora se lee con `chargeAppointQuery`
en cada sonda y se adopta **solo si el valor remoto ha cambiado** respecto al último visto: si
adoptara en cada lectura, una edición a medias quedaría pisada por la siguiente sonda antes de
poder aplicarla. `startTime` viaja en UTC, así que hace falta la conversión inversa: sin ella una
carga de las 03:00 se vería a las 01:00.

**El sondeo no tiene intervalo fijo.** Cargando, enchufado, en marcha, en marcha detenido y
parado tienen ritmos distintos; parado, por defecto, significa no tocar el coche. El bucle se
auto-reprograma, así que `PollController.schedule_next()` es el único sitio donde puede
detenerse. Una lectura que regrese tras descargar la integración intentaría rearmar el timer:
lo impide `TimerRegistry.close()`, que prohíbe cualquier `arm()` posterior.

**El PIN y la sesión son cosas distintas.** El token de la cuenta mueve sensores y lecturas; si
muere, toca reautenticar. El PIN de 4 cifras solo autoriza comandos remotos, y si es erróneo la
sesión sigue viva. Reautenticar no cambia el PIN, así que proponerlo sería el remedio
equivocado. El remedio se decide en `core/routing.py` sobre el código del backend, nunca sobre
el texto del mensaje.

**Cada PIN erróneo acerca el bloqueo de la cuenta.** `checkPassword` incrementa un contador del
lado de Chery, y ese bloqueo no se resuelve desde Home Assistant. Por eso `core/taskid.py`
falla antes de tocar el backend si el PIN está vacío, reutiliza el taskId mientras siga vivo, y
serializa el intento entero dentro de `PinLockout.attempt()`.

**Los snapshots fijan el registro de entidades.** `tests/snapshots/*.ambr` contiene `entity_id`,
`unique_id`, device class y unidades. Cambiar un `unique_id` deja huérfanas las entidades del
usuario y le borra el historial, así que un snapshot que se mueve suele ser una regresión.
Regenerar con `--snapshot-update` es válido cuando el cambio es intencionado, pero revisa el
diff antes.

## Trabajar aquí

```bash
cd my_develops/ebroAuto_homeAssistant
.venv-test/bin/pytest tests/ -n 4          # 668 tests, 274 snapshots
.venv-test/bin/ruff check custom_components tests
```

La suite usa su propio venv con `pytest-homeassistant-custom-component`, no el del repo core que
la rodea.

Si el cambio toca MQTT o el sondeo, conviene probarlo contra el coche antes de fusionar:
reiniciar HA, buscar `[auto] MQTT on_connect rc=0` en el log, comprobar que las entidades
conservan su `entity_id` y que un comando actualiza «Resultado del comando».
