# Ebro Auto — Integración para Home Assistant

Controla y consulta tu coche **Ebro** desde Home Assistant, sin depender de la app oficial.

⚠️ **Proyecto no oficial.** Uso estrictamente personal, sobre tu propio vehículo y tu propia cuenta.
No está afiliado ni respaldado por Ebro, Chery ni Home Assistant.

---

## Qué puedes hacer

**Ver** (sensores): batería, autonomía eléctrica y de combustible, cuentakilómetros total y
eléctrico, presión y
temperatura de los cuatro neumáticos, estado y tiempo de carga, ubicación en el mapa, y el estado de
puertas, ventanillas, maletero, capó, techo y cierre centralizado.

**Controlar** (necesita el PIN del vehículo): abrir y cerrar el coche, climatización con temperatura
ajustable, asientos y volante calefactables, desempañadores, maletero, ventanillas, techo, alarma,
carga programada con límite de batería, **parar una carga en curso**, localizar el coche por GPS
y hacerlo sonar y parpadear.

---

## Instalación

### Opción A — HACS (recomendada)
1. En Home Assistant: **HACS → Integraciones → menú (⋮) → Repositorios personalizados**.
2. Añade la URL de este repositorio con la categoría **Integración**.
3. Busca **Ebro Auto**, instálala y **reinicia** Home Assistant.

### Opción B — Manual
1. Copia la carpeta `custom_components/ebro/` dentro de `config/custom_components/`.
2. **Reinicia** Home Assistant.

---

## Configuración

Tras instalar y reiniciar: **Ajustes → Dispositivos y servicios → Añadir integración → Ebro Auto**.

### Paso 1 — tu cuenta

- **Teléfono** de tu cuenta Ebro, con el prefijo del país.
- **Contraseña** de la cuenta.
- **PIN** de comandos remotos: el mismo que te pide la app para abrir o cerrar el coche.

> ⚠️ **Mejor con la cuenta del titular del vehículo.** Con una cuenta secundaria todo funciona
> salvo una cosa: el coche no le envía avisos, así que puertas, cierre y maletero no se actualizan
> solos. Tiene solución —ver [Cuentas secundarias](#cuentas-secundarias)— y la integración te
> avisa sola si detecta que es tu caso.

Hay además un desplegable **«Avanzado»** con los servidores y el canal de Ebro. Viene relleno con
los valores correctos para Europa: no lo toques salvo que uses otra región o te lo pidan para
diagnosticar algo.

El VIN y el resto de datos se detectan solos al iniciar sesión. Home Assistant guarda la sesión y la
renueva por su cuenta: no tendrás que volver a escribir la contraseña salvo que caduque.

Las credenciales se guardan **únicamente en tu Home Assistant**. No hay ningún servidor intermedio.

### Paso 2 — cada cuánto consultar

La integración te pregunta cada cuánto pedir los datos que el coche no envía solo (batería,
autonomía, cuentakilómetros…). Puedes cambiarlo luego en **Configurar**. Todo en **minutos**, y
**0 significa no consultar** en ese estado:

| Cuando el coche está… | Por defecto |
|---|---|
| **Parado** | **0 — no se consulta** |
| **Enchufado** (cable puesto, aún sin cargar) | 30 min |
| **Cargando** | 15 min |
| **En marcha** | 3 min |
| **En marcha detenido** (semáforo, o te bajas con el coche encendido) | 5 min |
| **Tope enchufado sin cargar** (deja de consultar si el cable no llega a cargar) | 0 — sin límite |

Subir el valor de **Parado** es perfectamente razonable si quieres que los números se refresquen
también con el coche aparcado: consultar no despierta el vehículo. Ver
[Cómo se actualizan los datos](#cómo-se-actualizan-los-datos).

---

## Cómo se actualizan los datos

Hay **dos formas** de que un dato llegue a Home Assistant, y saber cuál es cada una explica por qué
unas cosas se actualizan solas y otras no.

### 1. Lo que el coche envía solo

En cuanto cambian, el coche los manda y aparecen al momento, incluso con «Actualización
automática» apagada:

- Puertas, ventanillas, maletero, capó, techo y **cierre centralizado**
- Climatización, desempañadores, volante y asientos: encendido o apagado
- **Cable de carga** conectado y **motor** en marcha

> **Con una cuenta secundaria, nada de esta lista se actualiza solo.** El coche solo envía
> avisos a la cuenta del titular. Ver [Cuentas secundarias](#cuentas-secundarias).

### 2. Lo que hay que consultar

Los **números** no los envía el coche; hay que pedirlos:

- Batería, autonomía, cuentakilómetros y velocidad
- Tensión, corriente, tiempo y estado de carga
- Presión y temperatura de neumáticos, consumos y avisos

Y aquí está lo importante: **cuando la integración consulta, le pregunta a la nube de Ebro, no al
coche**. La nube guarda el último estado que el vehículo le comunicó, y eso es lo que devuelve. Con
el coche despierto ese estado es de hace segundos; con el coche aparcado y dormido puede ser de hace
un buen rato, porque mientras duerme no informa de nada nuevo.

El sensor **«Resultado sonda de ubicación»** cuenta cómo fue la última consulta:

| Estado | Qué significa |
|---|---|
| 🟢 En vivo · coche despierto | El coche está conectado; el dato es de ahora mismo |
| 🟡 Estado desde la nube · hace N min · coche dormido | El coche duerme; esto es lo último que la nube guardó, y de cuándo es |
| 🟠 Con datos, sin posición | Llegó la telemetría, pero la nube no tiene una ubicación reciente. Se resuelve con «Localizar coche (GPS)» |
| 🔴 Sin datos · … | La nube no ha devuelto nada, con el motivo |
| ⏳ Lectura reciente · espero N s | Se acaba de consultar; se espera un poco antes de repetir |
| 🔑 Sesión caducada · vuelve a autenticarte | Hay que volver a iniciar sesión |
| 🛰️ Consultando… | En curso; dura un instante |

La posición merece fila propia porque puede faltar aunque el resto llegue: la nube solo la tiene si
el coche se la ha comunicado. Si te hace falta al momento, «Localizar coche (GPS)» se la pide.

### Consultar no despierta el coche

Solo lo despiertan los **mandos**: abrir, cerrar, climatización, «Localizar coche (GPS)»… Consultar
no le pide nada al vehículo, así que puedes ajustar los intervalos con tranquilidad.

La única excepción es el botón **«Actualizar estado completo»**: enciende la climatización durante
un minuto a propósito, porque es la única forma de leer batería y cuentakilómetros reales con el
coche frío. Es, con diferencia, el que más consume: úsalo con moderación.

De ahí también la diferencia entre los dos botones de ubicación: **«Actualizar ubicación»** trae la
última posición conocida por la nube, mientras que **«Localizar coche (GPS)»** hace que el coche
informe de dónde está ahora mismo.

### La consulta automática

La controla el interruptor **«Actualización automática»**, y el ritmo cambia según lo que esté
haciendo el coche:

- **Enchufado y Cargando:** se detecta por el cable y por si está entrando corriente. Enchufado
  consulta despacio y, al empezar a cargar de verdad, pasa al ritmo de carga.
- **En marcha:** circulando, el coche envía avisos muy seguidos; eso, con el sistema eléctrico de
  tracción («alta tensión») encendido, es lo que la integración entiende como «en marcha». Si dejan
  de llegar pero el coche sigue encendido (un semáforo, o te bajas sin apagarlo), pasa a «en marcha
  detenido».
- **Parado:** el coche apagado del todo. Si dejas ese intervalo en 0, no se consulta nada hasta que
  el coche vuelva a dar señales.

Con el coche frío ninguna consulta obtiene batería ni cuentakilómetros reales, por mucho que bajes
los intervalos: hace falta que el coche esté encendido.

---

## Carga programada

La **hora de inicio** y la **duración** muestran la programación que tiene el coche, y puedes
cambiarlas desde Home Assistant. La hora la pones en tu **hora local**; la conversión se hace sola.
La duración es un selector **HH:MM**, así que puedes poner por ejemplo 02:15.

Al cambiarlas **no se envían solas**: pulsa **«Aplicar carga programada»** (o vuelve a activar el
interruptor «Carga programada») para mandárselas al coche.

El sensor **«Carga programada en el coche»** enseña lo que hay puesto en el vehículo, incluso si lo
cambiaste desde la app oficial: la hora, si está activada y qué días se aplica.

## Programar la carga desde una automatización

Las entidades de hora y duración **guardan una preferencia**: no mandan nada al coche hasta que
pulsas «Aplicar carga programada». Es a propósito — así mover un control por accidente no le da
órdenes al coche.

Para automatizaciones hay una acción que lo hace todo de una vez:

```yaml
action: ebro.programar_carga
target:
  entity_id: switch.ebro_XXXX_carga_programada
data:
  hora_inicio: "02:00:00"
  duracion: 240        # minutos (mínimo 60: el coche rechaza menos)
  activar: true
```

Útil, por ejemplo, para cargar en las horas más baratas de luz sin tener que encadenar tres
llamadas y confiar en el orden.

**Para parar una carga en curso** usa el botón **«Parar carga»**. El interruptor de carga
programada apagado solo desactiva la programación; no corta una carga ya empezada.

Este coche **no permite arrancar ni parar la carga directamente** (su permiso «Control de Carga»
viene denegado de fábrica), así que «Parar carga» lo consigue imponiendo una programación cuya
ventana ya ha terminado. Funciona, pero tarda unos segundos en surtir efecto.

## Límite de carga

El coche no trae un tope de carga, así que la integración lo hace por su cuenta:

1. Enciende **«Limitar carga al porcentaje»** y elige el **«Límite de carga (%)»**, por ejemplo 80.
2. Mientras carga, la integración vigila la batería y corta la carga al llegar al objetivo.

Ten en cuenta que:

- Necesita **«Actualización automática» encendida** y el intervalo de **«Cargando» mayor que 0**:
  es lo que permite ir mirando la batería. La integración no te deja encender el límite si falta
  alguna de las dos, y te dice cuál; y si quitas una de las dos después, el límite se apaga solo
  — así no te quedas con un interruptor encendido que no está haciendo nada.
- Cerca del objetivo consulta más a menudo para afinar el corte, pero aun así puede pasarse un
  **1–2 %** según lo rápido que esté cargando.

---

## Cuentas secundarias

Si usas una **cuenta secundaria** —una a la que el titular del coche le ha dado acceso— hay una
diferencia que conviene conocer. La integración lo detecta sola y te lo avisa en **Ajustes →
Reparaciones**.

**Funciona con normalidad:** consultar el estado, la batería, la autonomía, la posición, y todos
los comandos — abrir, cerrar, climatización, maletero, ventanillas.

**No funciona:** el coche **no envía avisos por su cuenta** a las cuentas secundarias. Puertas,
cierre y maletero no se actualizan solos.

Es una limitación del servidor de Ebro, no de esta integración: la **app oficial tampoco recibe
esos avisos** con una cuenta secundaria. Lo que hace para parecer instantánea es **preguntar cada
pocos segundos**.

**Para que los estados se actualicen solos**, puedes hacer lo mismo: en las opciones de la
integración, pon un número distinto de 0 en **«Sondeo con el coche parado»**. Cuanto más bajo,
antes se refleja un cambio — y más peticiones se hacen a la nube de Ebro. Ten en cuenta que la app
solo pregunta mientras está abierta en pantalla, y Home Assistant lo haría a todas horas.

**Con la cuenta del titular** no hace falta nada de esto: los avisos llegan solos.

---

## Tarjeta para el panel (recomendada)

Para una vista completa del coche —imagen del vehículo, autonomías, neumáticos, accesos, clima,
carga y mapa— va muy bien la tarjeta **Vehicle Status Card** de
[@ngocjohn](https://github.com/ngocjohn/vehicle-status-card). Se instala desde **HACS → Frontend →
Repositorios personalizados**, con la categoría *Dashboard*.

En este repositorio tienes una lista para adaptar:
**[`examples/vehicle-status-card.yaml`](examples/vehicle-status-card.yaml)**.

1. Copia su contenido en una tarjeta manual: **Editar panel → Añadir tarjeta → Manual**.
2. **Sustituye `XXXX`** por los **cuatro últimos dígitos del VIN** de tu coche (buscar y reemplazar
   `ebro_XXXX_` por `ebro_1234_`).
3. La **imagen del coche** se elige en la interfaz de la tarjeta; en el YAML aparece marcada como
   `[ENLACE A IMAGEN LOCAL. CONFIGURAR EN GUI DE TARJETA]`. En `examples/` tienes `S900_General.png`
   y `S900_Top.png` (vista cenital, para los neumáticos).

---

## Privacidad

Todo el tráfico va directo entre tu Home Assistant y los servidores de Ebro/Chery, y tus
credenciales no salen de tu instalación.

## Agradecimientos

Basada en el excelente trabajo de
**[omoda9-ha](https://github.com/Caslinovich/omoda9-ha)**, adaptado a la marca **Ebro**.

## Licencia

MIT — ver [LICENSE](LICENSE).
