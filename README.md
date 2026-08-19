# Ebro Auto — Integración para Home Assistant

Integración personalizada (custom component) para controlar y monitorizar coches **Ebro**
(plataforma Chery "legend", tenant EU `euebro`) desde Home Assistant, sin depender de la app oficial.

⚠️ **Proyecto no oficial.** Uso estrictamente personal, sobre tu propio vehículo y tu propia cuenta.
No está afiliado ni respaldado por Ebro, Chery ni Home Assistant.

---

## Funcionalidades

- **Sensores:** nivel de batería, autonomía eléctrica, cuentakilómetros, presión y temperatura de
  neumáticos, combustible, estado de carga, ubicación (device_tracker), etc.
- **Sensores binarios:** puertas, ventanas, maletero, techo, capó, coche online, etc.
- **Control** (requiere PIN del vehículo): bloqueo/desbloqueo, climatización, asientos y volante
  calefactables/ventilados, desempañadores, carga programada, apertura de maletero/ventanas/techo,
  alarma antirrobo, localizar (GPS) y encontrar coche (luces).
- Actualización por **MQTT push** + sondeo periódico configurable.

---

## Instalación

### Opción A — HACS (recomendada)
1. En Home Assistant, ve a **HACS → Integraciones → menú (⋮) → Repositorios personalizados**.
2. Añade la URL de este repositorio y categoría **Integración**.
3. Busca **Ebro Auto**, instálala y **reinicia** Home Assistant.

### Opción B — Manual
1. Copia la carpeta `custom_components/ebro/` dentro de la carpeta `config/custom_components/` de tu Home Assistant.
2. **Reinicia** Home Assistant.

---

## Configuración

Tras instalar y reiniciar: **Ajustes → Dispositivos y servicios → Añadir integración → Ebro Auto**.

### Paso 1 — cuenta
- **Teléfono** de tu cuenta Ebro (con su prefijo de país).
- **Contraseña** de la cuenta.
- **PIN** de 4 dígitos del vehículo (el que usas en la app para comandos remotos).

⚠️ **Usa la cuenta PROPIETARIA del vehículo si quieres telemetría en tiempo real.** Con una cuenta
secundaria la integración inicia sesión, acepta comandos y conecta con el broker MQTT —la
suscripción incluso se concede— pero **por ese canal no llega nada**. Verificado en campo
(2026-08-18): la misma instalación, cambiando de la cuenta secundaria a la principal, pasó de cero
mensajes a telemetría normal, y al volver a la secundaria dejó de llegar otra vez.

Con una cuenta secundaria el estado sigue siendo consultable —la lectura REST funciona igual para
las dos— pero solo **cuando se pide**: hay que pulsar «Actualizar ubicación» o dejar activado el
sondeo periódico. Es lo que hace la app oficial cada vez que la abres, y por eso ahí sí parece
actualizarse sola.

El VIN y el resto de datos se detectan automáticamente tras el inicio de sesión. Home Assistant
guarda un token de sesión y lo **renueva solo** (no tendrás que reintroducir la contraseña salvo que
caduque la sesión).

### Paso 2 — intervalos de sondeo
Al pulsar **Siguiente**, la integración te pide **cada cuánto refrescar la telemetría** (batería,
autonomía, alta tensión, carga…) según el **estado** del coche. Vienen con valores por defecto y
puedes cambiarlos ahí o después en **Configurar** (Opciones). Todos en **minutos**; **0 = desactivado**
en ese estado:

| Intervalo | Cuándo se aplica | Por defecto |
|---|---|---|
| **Parado** | coche en reposo, alta tensión apagada | **0 = no se toca el coche** |
| **Enchufado** | cable conectado esperando (p. ej. carga programada aún sin empezar) | 30 min |
| **Cargando** | está entrando corriente de verdad | 15 min |
| **En marcha** | circulando (ráfaga de mensajes MQTT + alta tensión) | 3 min |
| **En marcha detenido** | alta tensión encendida pero sin ráfaga (semáforo, o te bajas con el coche en marcha) | 5 min |
| **Tope enchufado sin cargar** | corta el sondeo si el cable lleva ese tiempo sin llegar a cargar | 0 = sin límite |

Cómo se detecta cada estado y por qué estos ritmos → ver **[¿Cómo se actualizan los datos?](#cómo-se-actualizan-los-datos)**.

**Si usas una cuenta secundaria, «Parado» a 0 deja los estados de carrocería sin actualizarse.**
Puertas, cierre, maletero y techo cambian precisamente **con el coche parado**, y ahí es donde el
valor por defecto dice «no toques el coche». Con la cuenta propietaria da igual, porque esos
cambios llegan solos por MQTT; sin ese canal, la única forma de enterarse es preguntar. Ponlo a
5–10 minutos si quieres que se actualicen sin pulsar el botón, sabiendo que cada consulta con el
coche parado gasta batería de 12 V — por eso el valor por defecto es 0 y no un número cualquiera.

Otro efecto de no tener MQTT: **«En marcha» no llega a aplicarse nunca**, porque la marcha se
detecta por la ráfaga de mensajes del coche. Sin ráfaga, circular se clasifica como «en marcha
detenido» y manda ese intervalo.

> Las credenciales se guardan **solo en tu Home Assistant** (en el config entry). No hay ningún servidor intermedio.

---

## ¿Cómo se actualizan los datos?

Los datos del coche llegan por **dos canales distintos**. Entender la diferencia es clave para saber
qué se actualiza solo y qué no, y para cuidar la batería de 12 V.

### 🟢 Canal MQTT (push del coche) — en vivo y sin coste

El coche **empuja** estos estados en cuanto cambian, y la integración los recibe al instante (mantiene
una conexión MQTT permanente escuchando). **No gastan batería de 12 V** y funcionan **aunque
«Actualización automática» esté apagada**:

- Puertas, ventanillas, maletero, capó, techo, **cierre centralizado**
- Climatización y confort (desempañadores, volante, asientos…) encendido/apagado
- **Cable de carga conectado** y **Motor** (encendido/apagado)

### 🔴 Canal Realtime (hay que preguntar) — solo con sondeo

Estos **números** el coche NO los empuja; solo se actualizan cuando la integración **pregunta**
(sondeo automático o botones). Preguntar **contacta el coche** —no es gratis, aunque sea de solo
lectura— y tiene un pequeño coste de 12 V:

- Batería, autonomía, cuentakilómetros, velocidad
- Alta tensión, tensión/corriente, tiempo y estado de carga
- Presión y temperatura de neumáticos, consumos y avisos

### El sondeo automático por estados

Lo controla el interruptor **«Actualización automática»** (apagado por defecto) y es de **solo
lectura** (no ejecuta comandos ni fuerza la alta tensión; solo pregunta). El **ritmo depende del
estado** del coche; los **intervalos de cada estado se ajustan en el alta o en Opciones** (ver
[Configuración](#configuración)). Cómo se detecta cada estado:

- **Enchufado / Cargando:** por el **cable de carga** (evento MQTT) y, si entra corriente, por el
  estado de carga (realtime). *Enchufado* sondea lento y, al ver carga real, sube a *Cargando*.
- **En marcha:** al circular, el coche emite mensajes MQTT ("Último contacto") muy seguidos; con
  **≥5 en 30 s** + **alta tensión** encendida = *en marcha* (ritmo rápido). Si dejan de llegar
  mensajes pero la alta tensión sigue on (semáforo, o te bajas con el coche encendido) → *en marcha
  detenido*. Al **apagarse la alta tensión** → *parado* y deja de sondear.
- **Parado con 0:** no se contacta el coche hasta el siguiente evento MQTT (enchufar, o la ráfaga al
  arrancar a circular).

Con el coche en frío el sondeo **no** obtiene batería/cuentakilómetros reales (hace falta la alta
tensión encendida); para eso está el botón **«Actualizar estado completo»** (ver abajo).

### Cuidar la batería de 12 V

Hay **tres niveles de coste** de 12 V, de menor a mayor:

1. **Recibir MQTT** (puertas, cierre, cable…): **cero** — solo escuchamos, no pedimos nada.
2. **Sondeo automático** y botón **«Actualizar ubicación»**: **contacto ligero** — le pedimos datos y
   el coche responde. Pequeño coste, pero **no cero**. No enciende nada en el coche.
3. Botón **«Actualizar estado completo»**: **enciende el clima ~1 minuto** para forzar la alta
   tensión (única forma de leer batería/cuentakilómetros/tensión reales con el coche en frío) y luego
   lo apaga. Es el de **mayor consumo** — úsalo con moderación.

Con «Actualización automática» encendida y el estado **parado a 0**, en reposo **no se contacta el
coche**; solo se sondea cuando está enchufado o en uso, que es cuando esos números tienen sentido.

### Carga programada

La **hora de inicio** y la **duración** son preferencias: al cambiarlas **NO** se envían solas al
coche. Tras ajustarlas, pulsa el botón **«Aplicar carga programada»** (o vuelve a activar el
interruptor «Carga programada») para enviar el plan. La hora se envía en **UTC** automáticamente —
tú la pones en tu **hora local**. La **duración** es un selector **HH:MM** (horas y minutos, p. ej.
02:15).

### Límite de carga (%)

El coche no tiene un tope de carga nativo, así que la integración lo hace **por software**:

1. Enciende el interruptor **«Limitar carga al porcentaje»** y pon el **«Límite de carga (%)»** (ej. 80).
2. Mientras carga, la integración vigila la batería y, al alcanzar el objetivo, **para la carga**
   (imponiendo una programación fuera del horario actual, la única forma de parar en este coche).

Detalles:
- Requiere que **«Actualización automática» esté encendida** y el intervalo de **«Cargando» sea > 0**
  (es lo que permite leer la batería mientras carga).
- Al acercarse al objetivo, el sondeo se hace **más fino** (5 min) para ajustar mejor el corte; aun
  así puede pasarse **1–2 %** según la velocidad de carga.
- La batería se lee del canal realtime, así que este corte **contacta el coche** periódicamente
  mientras carga (coste de 12 V bajo: enchufado está en la red).

---

## Tarjeta para el panel (recomendada)

Para tener una vista completa del coche en Home Assistant (imagen del vehículo, autonomías,
neumáticos, accesos, clima, carga, mapa…) va muy bien la tarjeta **Vehicle Status Card** de
[@ngocjohn](https://github.com/ngocjohn/vehicle-status-card) — instálala desde **HACS → Frontend →
Repositorios personalizados** (categoría *Dashboard*).

En este repo tienes una tarjeta de ejemplo lista para adaptar:
**[`examples/vehicle-status-card.yaml`](examples/vehicle-status-card.yaml)**.

Para usarla:
1. Copia su contenido en una tarjeta manual (**Editar panel → Añadir tarjeta → Manual**).
2. **Sustituye `XXXX`** por los **últimos 4 dígitos del VIN** de tu coche en todos los
   identificadores de entidad (buscar/reemplazar `ebro_XXXX_` → `ebro_1234_`).
3. La **imagen del coche** (y opcionalmente el fondo de los neumáticos) se configura en la **GUI de
   la tarjeta**; en el YAML están marcadas como `[ENLACE A IMAGEN LOCAL. CONFIGURAR EN GUI DE TARJETA]`.
   En `examples/` tienes `S900_General.png` (coche) y `S900_Top.png` (vista cenital para neumáticos).

---

## Aviso de privacidad y seguridad

- Todo el tráfico va directo entre tu Home Assistant y los servidores de Ebro/Chery.
- El sondeo automático es de **solo lectura** (no ejecuta comandos), pero **sí contacta el coche**
  para leer, con un pequeño consumo de 12 V. No fuerza la alta tensión.
- El único control que **despierta de verdad** el coche es **«Actualizar estado completo»**, que
  **enciende el clima ~1 minuto** para encender la alta tensión. Ajusta los intervalos del sondeo en
  las **Opciones** de la integración (0 = desactivado).

## Agradecimientos

Esta integración está basada en el excelente trabajo de
**[omoda9-ha](https://github.com/Caslinovich/omoda9-ha)**, adaptado a la marca **Ebro**.

## Licencia

MIT — ver [LICENSE](LICENSE).
