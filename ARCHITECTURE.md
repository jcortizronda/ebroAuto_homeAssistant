# Arquitectura

Notas para trabajar en el código. Para instalar y usar la integración, ver `README.md`.

La integración habla con el coche por dos canales distintos, y de esa diferencia sale casi todo
el diseño:

- **MQTT** (mutual-TLS, `tspemqx-app-eu`): el coche empuja puertas, cierre, cable, motor. Llega
  solo y no cuesta nada.
- **REST** (`tspconsole-eu`): batería, autonomía, alta tensión, progreso de carga y los
  comandos. Hay que pedirlo — **a la nube, no al coche**: la sonda no lo despierta, y con el coche
  dormido responde con una instantánea de hace minutos u horas. Pedirlo de más no gasta batería
  del vehículo; lo que hace es competir con la app oficial, porque la nube de Chery solo admite
  una sesión por cuenta. Quien sí despierta el coche son los comandos.

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
| `services.py` | `ebro.programar_carga`: hora, duración y activación en una llamada. |
| `sensor.py`, `binary_sensor.py` | Lecturas del 5A02 y del canal realtime, desde tablas de specs. |
| `switch.py`, `climate.py`, `lock.py`, `cover.py`, `button.py` | Actuadores: estado + comandos en una entidad. |
| `number.py`, `time.py` | Preferencias locales. No mandan nada al coche — ver «Qué actúa y qué guarda». |
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

## Glosario

Los nombres del código están en inglés —es la convención de Home Assistant y de Python, y lo que
hace que alguien que venga de otra integración se oriente— pero las explicaciones están en
castellano. Esta tabla es el puente entre las dos cosas: qué es cada término cuando aparece en el
código, en un mensaje de commit o en una conversación.

### Los dos canales

| Término | Qué es |
|---|---|
| **MQTT** / *push* | El coche **envía** por su cuenta cuando algo cambia. Gratis e inmediato, pero solo mientras está despierto. Trae puertas, cierre, maletero, techo, cable y motor. |
| **5A02** | El tipo de mensaje MQTT que trae ese estado de carrocería. Si lees «un 5A02», es «un aviso del coche». |
| **1301** | El tipo de mensaje MQTT que traería la **posición**. Este coche no lo envía nunca. |
| **110D** | Un mensaje MQTT de **confirmación de comando**: el coche acusa que ha ejecutado algo. |
| **realtime** | El bloque de datos que devuelve la nube cuando le **preguntamos**: batería, autonomía, odómetro, neumáticos. |
| **`fields`** | Lo último que llegó por MQTT. Se acumula y **nunca se vacía**, así que con el coche dormido es historia. |
| **sonda** / *probe* | Una consulta a la nube. **No despierta el coche**: pregunta al servidor, no al vehículo. |

### La sonda y el bucle

| Término | Qué es |
|---|---|
| **`async_probe()`** | Ejecutar una sonda. La usan el botón «Actualizar ubicación», el bucle y el flanco de despertar. |
| **`force=True`** | «Sáltate el margen de espera entre sondas». Lo usan los caminos automáticos y el botón manual. |
| **cooldown** | El margen de 120 s entre sondas, para no machacar la nube. |
| **`schedule_next()`** | Decidir **cuándo será la siguiente** sonda y programarla. Es el único sitio donde el bucle puede pararse. |
| **`hv_followup`** | La línea que `schedule_next()` escribe en el diagnóstico. **Una línea = una sonda completada.** |
| **`every_s`** | En esa línea, los segundos hasta la **próxima** sonda. NO es lo que se ha tardado. |
| **ráfaga** / *burst* | Cinco o más mensajes MQTT en 30 s. Es como se detecta que el coche circula. |
| **AT** / *alta tensión* | La batería de tracción encendida. Es lo que distingue «en marcha» de «parado». |

### Comandos

| Término | Qué es |
|---|---|
| **comando** | Una **orden** al coche: abrir, cerrar, clima, localizar. Sí lo despierta. Requiere PIN. |
| **`taskId`** | El permiso temporal que da el backend tras validar el PIN. Sin él no se ejecuta ningún comando. |
| **`checkPassword`** | La llamada que valida el PIN y entrega el `taskId`. **Cada fallo acerca el bloqueo de la cuenta.** |
| **anti-bloqueo** | El freno propio que deja de preguntar tras varios PIN rechazados, para no llegar a ese bloqueo. |
| **optimismo** | Tras un comando, la entidad muestra el objetivo antes de que el coche confirme, para que la interfaz no se quede quieta. |
| **`queryVehicleLocation`** | **Consulta** la última posición que sabe la nube. No pide nada al coche. |
| **`vehicleLocation`** | El **comando** «Localizar coche (GPS)». Despierta el coche para que reporte dónde está. |

### Piezas del código

| Término | Qué es |
|---|---|
| **coordinator** | La pieza central: tiene el estado del coche y lo reparte a todas las entidades. |
| **entidad** / *entity* | Cada cosa que ves en Home Assistant: un sensor, un interruptor, un botón. |
| **plataforma** / *platform* | El archivo que crea las entidades de un tipo: `sensor.py`, `switch.py`, `lock.py`… |
| **`unique_id`** | El identificador que ata una entidad a su histórico. **Cambiarlo borra el historial del usuario.** |
| **snapshot** | Una foto guardada de cómo quedan las entidades. Si un test de snapshot falla, algo se ha movido. |
| **config entry** | La configuración de la integración: credenciales, VIN, intervalos. |
| **Repair** | Un aviso accionable de Home Assistant, con su botón para arreglar lo que sea. |

## Cosas que conviene saber antes de tocar

**El estado del coche está tras un lock, y no se puede sortear.** Lo tocan tres hilos: paho,
los executors y el bucle de eventos. `VehicleState` no expone sus dicts, devuelve copias.
`record_message()` resuelve los campos y el flanco de despertar en la misma operación, porque
leerlos por separado deja una ventana en la que el flanco se pierde.

**Los dos canales traen las mismas claves, y manda el push.** `doorLock`, `trunkDoor`,
`frontHVACState` y las puertas vienen tanto en el push 5A02 como en la sonda realtime.
`helpers.field()` usa el push y solo cae a la sonda si el push no trae la clave — que es el caso
de las cuentas sin MQTT, para las que existe el respaldo.

Hubo una versión que invertía la prioridad con el coche dormido, razonando que `fields` se
acumula y nunca se vacía. Salió mal: la instantánea de la nube también es historia, y puede ser
más vieja. Medido el 04/09/2026 — el maletero se cerró, el push lo reflejó, y 2 ms después de
vencer la ventana de «despierto» la entidad volvía a «abierto» con el `trunkDoor=1` de la
instantánea. Un push es un EVENTO; la instantánea es una caché.

**El estado optimista caduca con la verdad, venga por donde venga.** Tras un comando la
entidad muestra el objetivo, porque el coche tarda en confirmar. Ese objetivo cede en cuanto
llega un push MQTT (`last_seen`) **o** una sonda con contenido distinto (`car_data_ts`).
Anclarlo solo a MQTT dejaba el objetivo clavado para siempre en un coche que no empuja.

**El canal MQTT solo existe para la cuenta PROPIETARIA del vehículo.** El topic va contra el id
de usuario (`app/<canal>/<tuserid>/account/msgCenter/msg`). Con una cuenta delegada el broker
acepta la conexión y **concede la suscripción** —`car_subscribed: true`, «Granted QoS 1»— y ahí
no se publica jamás nada: ni los avisos del coche, ni siquiera la confirmación de un comando que
esa misma cuenta acaba de ejecutar con éxito.

**No hay ningún topic que buscar, y esto hay que leerlo entero antes de intentarlo.** Una versión
anterior de este documento decía que la app oficial, con esa misma cuenta delegada, sí recibía los
avisos al instante, y concluía que existía un topic desconocido. **Es falso.** Un análisis del
tráfico de la app (01/10/2026, con VPN y descifrado del HTTPS) midió que:

* con la cuenta delegada **el broker RECHAZA el CONNECT de la propia app oficial**: responde 4
  bytes y cierra a los ~0,5 s, y la app reintenta en bucle cada ~3,6 s indefinidamente;
* lo que hace la app para parecer instantánea es **preguntar a `/asr/manager/realtime` cada 5,0
  segundos**. Los avisos en pantalla salieron 0,5–1,2 s después de la respuesta en la que
  `doorLock` cambiaba;
* con la cuenta titular el broker sí acepta, y cada conexión recibe un volcado de ~9,7 kB al
  conectar.

La conclusión práctica: **la diferencia no está en nuestro cliente.** Se descartaron una a una,
con medidas, dieciséis hipótesis —el id de cuenta, el coche predeterminado, `passwordType`, el
`authorizeType`, el clientId exacto de la app (que además tira la conexión en bucle), los campos
de identificación de dispositivo, la ventana de la delegación y la lista de 329 permisos, que solo
difiere en «Seguridad», «Ajustes de seguridad», «Zona de seguridad» y «Enviar al coche»—. Ninguna
era la causa, porque no hay nada roto que arreglar: para una cuenta delegada ese canal no existe.

Lo que la integración hace al respecto es **avisar**: `coordinator.async_review_delegated_account`
levanta una Reparación cuando `authorizeType == 1`, explicando que los estados no se moverán solos
y que la salida es configurar los intervalos de consulta. No es reparable porque no hay nada que
reparar.

Por eso el diagnóstico distingue conectado de suscrito: sin esa distinción, la única pista era un
`fields_count: 0` que también significa «el coche está dormido».

**Qué ACTÚA y qué GUARDA.** Los botones, interruptores, candados, persianas y el clima mandan
comandos: tocarlos es consentimiento explícito. Los `number` y los `time` solo guardan una
preferencia que otro control usará al enviar. Por eso programar la carga son tres pasos (hora,
duración, «Aplicar») en vez de uno: se valoró hacer que las horas escribiesen directas al coche y
se descartó, porque mover un control por accidente le daría órdenes.

Esa separación es buena para la interfaz y mala para una automatización, que tendría que encadenar
tres llamadas y confiar en el orden. De ahí `ebro.programar_carga`: una llamada a un servicio es
un acto explícito igual que pulsar un botón, así que respeta el mismo principio. Y por el mismo
criterio, «Parar carga» es un BOTÓN y no un servicio — no lleva parámetros, y un servicio sin
campos no se puede poner en una tarjeta ni sale en la página del dispositivo.

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

**El PIN no tiene longitud fija.** La app lo pide de 6 dígitos y el campo nunca ha validado
ninguna longitud; la documentación decía «PIN de 4 dígitos», y eso llevó a dudar de la longitud
cuando el problema era otro.

**Sin confirmar: el PIN podría ser POR CUENTA y no del vehículo.** Observado el 01/10/2026 sobre
el mismo coche — con la cuenta titular funcionaba un PIN y con una delegada otro distinto, después
de cambiarlo desde la app con la titular. Encaja con «cada cuenta tiene el suyo», pero **nunca se
hizo la prueba limpia** (cambiarlo desde la delegada y ver a cuál afecta), así que no se afirma en
ninguna parte de cara al usuario. Queda apuntado porque, si es cierto, explica un desconcierto
caro: se cambió el PIN en una cuenta, falló en la otra, y todo lo demás pareció encajar durante
doce horas en una teoría equivocada. Si alguien lo comprueba, que lo escriba aquí.

**`checkPassword code=1` con `message` `A07908`/`A07909` significa PIN incorrecto.** Medido. Se
reclasificaron unas horas como «no es el PIN» razonando que la app aceptaba el PIN que a nosotros
nos fallaba; la premisa era falsa (la app aceptaba el PIN NUEVO de otra cuenta) y el efecto era
quitarle el freno al anti-bloqueo ante un PIN realmente erróneo. Esa clase de error no la paga el
software: la paga la cuenta del usuario. Por eso los dos códigos están explícitos en
`routing._OVERRIDE_CHECKPASSWORD` y en el test exhaustivo de `counts_for_lockout`.

**El PIN y la sesión son cosas distintas.** El token de la cuenta mueve sensores y lecturas; si
muere, toca reautenticar. El PIN de comandos solo autoriza comandos remotos, y si es erróneo la
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
.venv-test/bin/pytest tests/ -n 4          # 675 tests, 274 snapshots
.venv-test/bin/ruff check custom_components tests
```

La suite usa su propio venv con `pytest-homeassistant-custom-component`, no el del repo core que
la rodea.

Si el cambio toca MQTT o el sondeo, conviene probarlo contra el coche antes de fusionar:
reiniciar HA, buscar `[auto] MQTT on_connect rc=0` en el log, comprobar que las entidades
conservan su `entity_id` y que un comando actualiza «Resultado del comando».
