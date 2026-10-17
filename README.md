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

### Panel web

```bash
python run.py -m <MINT1> -m <MINT2> --web          # http://127.0.0.1:8080
python run.py -m <MINT> --web 9000                 # otro puerto
```

Muestra en vivo todas las posiciones (estado, precio, próximo nivel de
activación o de venta, PnL), el detalle de cada una y los logs filtrables
por token. Desde ahí se puede **cerrar** una posición (vende al precio
actual, o cancela la entrada si todavía no compró) o cerrarlas todas,
siempre con confirmación. Abrir posiciones desde la web llegará más
adelante.

Por defecto solo escucha en `127.0.0.1` (esta máquina). Para verlo desde
la red local, `--web-host <IP de la máquina>` (o `0.0.0.0`). Cuando terminan
todas las posiciones, el proceso sale y el panel se cierra con él.

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

Cada orden cerrada se registra en el CSV de `general.trade_history_csv`
(`""` desactiva el historial).

## Fuentes de precio

El feed en vivo de PumpPortal es la fuente principal. Hay dos fallbacks
on-chain que se activan solos cuando ese feed deja de entregar trades:

- **Bonding curve** (`PumpCurveOnChainClient`): lee las reservas virtuales
  de la cuenta PDA de pump.fun para mints que todavía no migraron.
- **PumpSwap** (`PumpSwapOnChainClient`): lee las reservas del pool para
  mints ya migrados. `subscribeTokenTrade` no cubre esos mints y deja de
  mandar trades en silencio, así que el bot lo detecta por timeout
  (`stall_timeout_seconds`) y lo confirma on-chain antes de cambiar de
  fuente.

No se usan DexScreener ni Jupiter a propósito: desalinean el precio que el
bot "ve" del precio contra el que realmente se ejecuta la orden.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

No se toca la red en ningún test: el websocket, la Lightning API y el RPC
de Solana están todos mockeados.
