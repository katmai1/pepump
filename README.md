# pepump

Bot de *trailing take-profit* para un token de pump.fun / PumpSwap, operando
vía la Lightning Transaction API de PumpPortal.

⚠️ Herramienta de trading, no consejo financiero. Las memecoins de pump.fun
son extremadamente volátiles. Prueba siempre primero con `live = false` y,
después, con importes pequeños.

## Requisitos

- Python 3.11+ (`tomllib` viene incluido desde 3.11).
- Una API key de PumpPortal asociada a una wallet con al menos 0.02 SOL.
  Es **obligatoria siempre**, incluso con `live = false`: el precio sale
  únicamente de `subscribeTokenTrade`, que es un stream medido.
- Recomendado: un RPC de Solana propio (Helius, QuickNode, etc.). El
  endpoint público `api.mainnet-beta.solana.com` corta por rate limit en
  `getProgramAccounts`, que es lo que se usa para encontrar el pool de
  PumpSwap de un mint ya migrado.

```bash
pip install -r requirements.txt
```

## Configuración

```bash
cp config.toml.example config.toml
```

Edita `config.toml` (está en `.gitignore`, no se versiona). La API key se
puede dejar vacía en el archivo y pasarla por entorno:

```bash
export PUMPPORTAL_API_KEY="..."
```

El `mint` **no** va en el `.toml`: se pasa por línea de comandos.

## Uso

```bash
python run.py -m <MINT> -c config.toml      # modo según general.live
python run.py -m <MINT> -v                  # logging en DEBUG
python run.py -m <MINT1> -m <MINT2>         # varias posiciones a la vez
python run.py                               # solo retomar las que quedaron abiertas
```

Con varios `-m`, todas las posiciones corren en el mismo proceso y
comparten una única conexión al feed de PumpPortal. El panel muestra una
línea por posición y cada log lleva la etiqueta de su token. No se puede
repetir un mint: las posiciones comparten wallet, y la venta pide el 100%
de los tokens de ese mint.

### Salir y retomar

`Ctrl+C` (o `SIGTERM`) **sale sin vender**: las posiciones abiertas quedan
guardadas en `pepump.db` (ver `general.state_db`) y, al volver a arrancar el
bot, se retoman donde estaban (entrada, máximo, trailing armado o no), sin
volver a comprar. **Mientras el bot está parado nadie vigila el stop**: si
el precio cayó por debajo del nivel de venta, vende nada más retomarla.
Las que todavía esperaban entrada se cancelan (no había dinero en juego).

Con `--sell-on-exit`, `Ctrl+C` vende todas al precio actual antes de salir
(el comportamiento de antes).

Lo mismo vale si el proceso muere de golpe (corte de luz, `kill -9`,
`taskkill /F` en Windows): al arrancar se retoma lo que estaba abierto. En
modo real, antes de retomar se mira la wallet: si los tokens ya no están
(vendidos a mano), la posición se da por cerrada. Si el bot murió justo
mientras compraba, se mira si la compra entró; si entró, se marca para
revisar a mano (no hay datos de entrada para aplicar la estrategia).

Una posición abierta en modo real solo se retoma arrancando en modo real,
y lo mismo con el simulado.

En Windows `Ctrl+C` y `Ctrl+Pausa` funcionan igual; `SIGTERM` no se
entrega nunca.

### Panel web (el bot como servicio)

```bash
python run.py --web                    # http://127.0.0.1:8080
python run.py --web 9000               # otro puerto
python run.py --web -m <MINT>          # y además abre esta posición al arrancar
```

Con `--web` el bot se queda en marcha aunque no haya posiciones (retoma las
que quedaron abiertas) y todo se hace desde el navegador:

- **Nueva posición:** eliges si es **simulada o real**, pegas el mint y
  muestra qué token es (nombre, si sigue en la curva de pump.fun o ya
  migró, precio actual) antes de comprar. El importe y los ajustes salen del
  `.toml`; se pueden cambiar para esa posición en "Ajustes avanzados".
  Siempre con confirmación (en rojo si es real).
- **Cerrar** una posición (vende al precio actual, o cancela la entrada si
  todavía no compró) o **cerrarlas todas**, también con confirmación.
- En vivo: estado, precio, próximo nivel de activación o de venta, PnL y
  logs filtrables por token. Al pulsar una posición, su **gráfico de
  precio** con la entrada, la activación, el stop y el nivel de venta del
  trailing, y las marcas de compra, armado y venta (los precios se guardan:
  el gráfico sobrevive a un reinicio). Tras vender, el precio se sigue
  registrando 30 minutos más, leído on-chain y no por el feed (que se paga
  por evento), para poder analizar después qué habría pasado con otros
  ajustes. Solo mientras el proceso siga en marcha: sin `--web`, el bot sale
  en cuanto se cierran las posiciones.
- **Historial** (pestaña de arriba): todas las posiciones terminadas.
- **Saldo de la wallet** arriba a la derecha, y antes de cada compra real se
  comprueba que haya SOL suficiente. Para verlo desde el arranque, pon la
  dirección pública de tu wallet en `wallet_address` (sección
  `[pumpportal]`); si no, el bot la aprende en la primera compra real.

Solo se sale con `Ctrl+C` (sin vender, salvo `--sell-on-exit`).

**Real o simulada, por posición:** el formulario y `ctl.py open` empiezan
siempre en simulado; lo real se elige a propósito cada vez. `general.live` del
`.toml` solo decide el modo de las posiciones de `-m`. Cada posición lleva su modo, se
marca con **REAL** en la tabla, y se retoma en ese modo al reiniciar. Las
cifras de arriba nunca suman dinero real con simulado.

**Límites de seguridad** (en el `.toml`, no se pueden cambiar desde la web):
`max_buy_sol` (máximo por posición, 0.5 SOL por defecto) y
`max_open_positions` (posiciones activas a la vez, 10 por defecto). Pasarse
da error y no compra, venga la orden de la web, de `ctl.py` o de `-m`.

Por defecto solo escucha en `127.0.0.1` (esta máquina). Para verlo desde
la red local, `--web-host <IP de la máquina>` (o `0.0.0.0`).

**Contraseña (opcional):** `web_password` en la sección `[web]` del `.toml`,
o la variable de entorno `PEPUMP_WEB_PASSWORD`. Sin ella, cualquiera que
llegue al puerto puede cerrar posiciones; si el panel escucha en la red sin
contraseña, el bot lo avisa al arrancar. La web va por HTTP sin cifrar: en
una red que no sea de confianza, mejor Tailscale o un túnel SSH
(`ssh -L 8080:127.0.0.1:8080 tu-servidor`).

### Desde otra consola

Con el bot corriendo con `--web`:

```bash
python ctl.py list                 # posiciones y su estado
python ctl.py open <MINT>          # abrir con los ajustes del .toml
python ctl.py open <MINT> --sol 0.1 --trailing 12   # ... o con otros
python ctl.py open <MINT> --real   # con SOL real (sin --real, simulada)
python ctl.py close 2              # cerrar la #2 (o: close <MINT>)
python ctl.py close-all            # cerrar todas
python ctl.py --url http://192.168.1.50:9000 list
```

Pide confirmación (`-y` para saltarla) y, si el panel tiene contraseña, la
toma de `PEPUMP_WEB_PASSWORD` o la pregunta.

## Estrategia

1. Se suscribe al feed de trades del mint **antes** de comprar y espera el
   primer trade real para fijar el precio de referencia.
2. Con `entry_dip_pct = 0` compra en esa referencia. Con `entry_dip_pct > 0`
   espera a que el precio baje ese porcentaje antes de entrar (sin timeout).
3. Mientras el precio no suba `activation_pct` % desde la entrada, solo
   vigila un stop-loss duro (`initial_stop_pct`).
4. Al superar `activation_pct`, el trailing-stop se arma: el nivel de venta
   sigue al máximo alcanzado, siempre `trailing_pct` % por debajo.
5. Si el precio retrocede hasta ese nivel, vende.

Si una venta falla (lo típico: slippage excedido con el precio cayendo), se
reintenta con el siguiente precio, tras una pausa corta y creciente, subiendo
en cada intento el slippage (+10 puntos) y el priority fee (x4) hasta
`max_sell_slippage` / `max_sell_priority_fee`. Con 3 fallos seguidos avisa
en el log y en el panel web.

En modo simulado se cobran por defecto los costes que tendría la operación
en real (comisiones de pump.fun y PumpPortal, un 1% de slippage en contra,
priority fee y rent de la cuenta del token), para que el PnL simulado no
salga más optimista que el real. Se ajustan con `sim_fee_pct` /
`sim_slippage_pct`, o se apagan con `sim_costs = false` (sección `[general]`).

**Rent de la cuenta del token.** Cada compra crea una cuenta para el token,
con ~0.0015-0.002 SOL de rent (un 7-10% de una compra de 0.02 SOL). Ni
PumpPortal ni Jupiter la cierran al venderlo todo: el rent se queda dentro y
el historial lo registraba como pérdida. Con `wallet_private_key`, el bot
cierra la cuenta tras cada venta real y suma el rent recuperado a lo
recibido; al arrancar, cierra también las cuentas vacías que quedaron de
antes (`close_empty_token_accounts`, sección `[jupiter]`). El rent no cuenta
en el precio de entrada ni en el PnL neto mientras la posición está abierta.

Cada orden cerrada se registra en el CSV de `general.trade_history_csv`
(`""` desactiva el historial).

## Fuentes de precio

El feed en vivo de PumpPortal es la fuente principal. Hay dos fallbacks
on-chain que se activan solos cuando ese feed deja de entregar trades:

- **Bonding curve** (`PumpCurveOnChainClient`): lee las reservas virtuales
  de la cuenta PDA de pump.fun para mints que todavía no migraron.
- **PumpSwap** (`PumpSwapOnChainClient`): lee las reservas del pool para
  mints ya migrados. `subscribeTokenTrade` no cubre esos mints. Al abrir la
  posición, el bot mira on-chain en paralelo con el feed y, si el mint ya
  migró (o cotiza en Raydium CPMM / Meteora), usa ese precio enseguida sin
  esperar al feed. Si migra a mitad de posición, el feed deja de mandar
  trades en silencio: el bot lo detecta por timeout
  (`stall_timeout_seconds`) y lo confirma on-chain antes de cambiar de
  fuente.
- **Raydium CPMM / Meteora** (`RaydiumCpmmOnChainClient`,
  `MeteoraDlmmOnChainClient`, `MeteoraDammV2OnChainClient`): mints que no
  son de pump.fun pero cotizan contra SOL. En Meteora se miran DLMM y DAMM v2
  y se usa el pool con más SOL: un DLMM pequeño da un precio que solo se
  mueve a saltos de un bin (2% con `bin_step` 200). La Lightning API no
  tiene un pool de Meteora, así que con `jupiter_api_key` (y la clave
  privada, en real) las órdenes de estos tokens van por Jupiter; sin ella,
  con `pool = "auto"`.

No se usan DexScreener ni Jupiter para el precio de estos tokens a propósito:
desalinean el precio que el bot "ve" del precio contra el que realmente se
ejecuta la orden.

### Tokens que no cotizan contra SOL: Jupiter

Algunos tokens solo tienen liquidez contra otro token: los de StonkFun, por
ejemplo, están en una curva de Raydium LaunchLab contra STONK, ORE o una
acción tokenizada (TSLAx). Si un mint no aparece en ninguna de las fuentes de
arriba y hay `jupiter_api_key` (sección `[jupiter]`), el bot lo opera **por
Jupiter**:

- **Precio:** lo que Jupiter daría ahora por vender la posición, dividido
  entre los tokens. Ya incluye la ruta (SOL → STONK → token), las comisiones
  de los pools y de Jupiter, el impacto y el impuesto de transferencia de
  Token-2022. Antes de comprar, se cotiza la venta de lo que se compraría con
  `buy_sol`.
- **Órdenes:** por Jupiter (`/order` + `/execute`). Jupiter no firma: para
  operar en real hace falta la clave privada de la wallet
  (`wallet_private_key` en `[jupiter]`, o la variable de entorno
  `PEPUMP_WALLET_PRIVATE_KEY`), y tiene que ser la misma wallet que la de
  PumpPortal. Sin ella, Jupiter solo se usa en simulado. Con la clave en el
  `.toml`, quien lea ese archivo puede vaciar la wallet: `chmod 600
  config.toml` (el bot avisa al arrancar si lo pueden leer otros usuarios).
- **Simulado:** la compra usa la cotización real de Jupiter, y la venta no
  cobra `sim_fee_pct` (el precio ya es neto).
- **Cupo de peticiones:** el plan gratuito da una petición cada ~2 s para
  toda la cuenta (`jupiter_request_interval_seconds`). Las órdenes van
  siempre delante de las consultas de precio; con varias posiciones por
  Jupiter, el precio de cada una se refresca más despacio.

**Todas las órdenes por Jupiter** (`jupiter_for_all = true`): también las de
pump.fun (curva) y PumpSwap. Jupiter cobra un 0.1% (0.5% en tokens de menos
de 24 h) y la Lightning API de PumpPortal un 1%; la comisión del pool se paga
igual. Medido con 0.05 SOL en tokens de PumpSwap: ~0.9 puntos menos por
operación. El precio sigue saliendo del feed de PumpPortal u on-chain; solo
cambia por dónde va la orden (en la web: "Órdenes por"). En real hace falta
`wallet_private_key`; sin ella, siguen yendo por PumpPortal.

Ojo con los costes: comprar y vender al momento sale alrededor de un 8% más
caro en estos tokens (impuestos de transferencia, comisiones de la curva,
dos saltos de ruta). El PnL neto arranca en ese negativo.



## Tests

```bash
pip install -r requirements-dev.txt
pytest
ruff check .     # lint (config en pyproject.toml)
mypy             # tipos
```

No se toca la red en ningún test: el websocket, la Lightning API y el RPC
de Solana están todos mockeados.
