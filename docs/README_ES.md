<div align="center">

# Wyckoff Trading Agent

**Sistema cuantitativo Wyckoff para acciones A de China — hablas con naturalidad, el agente lee el mercado**

[![PyPI](https://img.shields.io/pypi/v/youngcan-wyckoff-analysis?color=blue)](https://pypi.org/project/youngcan-wyckoff-analysis/)
[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-AGPL--3.0-green.svg)](../LICENSE)
[![Streamlit](https://img.shields.io/badge/demo-Streamlit-FF4B4B.svg)](https://wyckoff-analysis-youngcanphoenix.streamlit.app/)

[中文](../README.md) | [English](README_EN.md) | [日本語](README_JA.md) | [한국어](README_KO.md) | [Arquitectura](ARCHITECTURE.md)

</div>

---

Conversa en lenguaje natural con un agente IA experto en el metodo Wyckoff. Despacha de forma autonoma 10 herramientas profesionales + 5 capacidades generales, encadena razonamientos de multiples pasos y te da conclusiones accionables de trading.

Web + CLI + MCP triple canal, compatible con Gemini / Claude / OpenAI / DeepSeek, automatizacion completa via GitHub Actions.

## Funcionalidades

| Capacidad | Descripcion |
|-----------|-------------|
| Agente conversacional | Diagnosticos, filtros y reportes activados con lenguaje natural; el LLM orquesta herramientas de forma autonoma; tambien lee/escribe archivos, ejecuta comandos y obtiene contenido web |
| Skills | Comandos slash integrados (`/screen`, `/checkup`, `/report`, `/strategy`, `/backtest`) para flujos complejos con un toque; extensible por el usuario via `~/.wyckoff/skills/*.md` |
| Embudo de 5 capas | ~4 500 acciones del mercado completo se reducen a ~30 candidatas mediante seis canales + resonancia sectorial + micro-disparo |
| Reporte IA de 3 campamentos | Logica rota / Reserva / Plataforma de despegue — el LLM clasifica de forma independiente |
| Diagnostico de cartera | Escaneo masivo: estructura de medias moviles, fase de acumulacion, senales de activacion, estado de stop-loss |
| Rebalanceo privado | Combina posiciones + candidatas y emite ordenes EXIT / TRIM / HOLD / PROBE / ATTACK, con push a Telegram |
| Estrategia de compra al cierre | Ejecuta a las 13:50, evaluacion en dos etapas (puntuacion por reglas + revision LLM) para entradas al final del dia |
| Confirmacion de senales | Las senales L4 pasan por 1-3 dias de confirmacion de precio antes de ser accionables |
| Seguimiento de recomendaciones | Sincroniza automaticamente el precio de cierre y calcula el rendimiento acumulado |
| Backtesting | Simula rendimiento a N dias tras el filtrado del embudo: tasa de aciertos, Sharpe, drawdown maximo |
| Riesgo pre-mercado | Monitoreo de A50 + VIX con cuatro niveles de alerta |
| Panel local | `wyckoff dashboard` — recomendaciones, senales, cartera, memoria del agente, logs de chat; tema oscuro/claro, bilingue CN/EN |
| Memoria del agente | Memoria entre sesiones: extrae conclusiones automaticamente, inyecta contexto relevante en la siguiente consulta |
| Compresion de contexto | Umbral dinamico (25% de la ventana de contexto del modelo) para compresion automatica, resumen inteligente de resultados de herramientas |
| Confirmacion de herramientas | `exec_command`, `write_file`, `update_portfolio` requieren aprobacion del usuario antes de ejecutarse |
| Capacidades generales del Agent | Ejecutar comandos, leer/escribir archivos, obtener paginas web — envia una ruta CSV y lo analiza |
| MCP Server | 10 herramientas expuestas via protocolo MCP — compatible con Claude Code / Cursor / cualquier cliente MCP |
| Notificaciones multicanal | Feishu / WeCom / DingTalk / Telegram |

## Fuentes de datos

La descarga de datos diarios se degrada automaticamente:

```
tickflow → tushare → akshare → baostock → efinance
```

Si una fuente no esta disponible, se cambia automaticamente a la siguiente sin intervencion manual.

> **Recomendado: conectar TickFlow para capacidades mas fuertes en tiempo real / intradias**
> Registro: [Enlace de registro TickFlow](https://tickflow.org/auth/register?ref=5N4NKTCPL4)

## Inicio rapido

### Instalacion en una linea (recomendado)

```bash
curl -fsSL https://raw.githubusercontent.com/YoungCan-Wang/Wyckoff-Analysis/main/install.sh | bash
```

Detecta Python, instala uv y crea un entorno aislado. Al finalizar, ejecuta `wyckoff`.

### Homebrew

```bash
brew tap YoungCan-Wang/wyckoff
brew install wyckoff
```

### pip

```bash
uv venv && source .venv/bin/activate
uv pip install youngcan-wyckoff-analysis
wyckoff
```

### Comenzar a usar — Configuracion del Agent en un clic

Solo dos pasos tras el inicio:
1. `/model` — elegir modelo (Gemini / Claude / OpenAI) e introducir API Key
2. Empieza a preguntar — sin registro, los datos de cartera se guardan localmente

```
> Compara 000001 y 600519, cual conviene comprar?
> Revisa mi cartera
> Como esta el mercado general ahora?
```

> Opcional: `/login` para sincronizar cartera en la nube (multi-dispositivo). Todas las funciones estan disponibles sin login.

Actualizar: `wyckoff update`

### Grid de backtest

18 combinaciones de parametros en paralelo, salida de parametros optimos, matriz Sharpe y revision de estrategia:

| Parametros optimos & Ranking | Matriz de parametros |
|:---:|:---:|
| <img src="../attach/backtest-grid-1.png" width="450" /> | <img src="../attach/backtest-grid-2.png" width="450" /> |

### Web

```bash
git clone https://github.com/YoungCan-Wang/Wyckoff-Analysis.git
cd Wyckoff-Analysis
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
streamlit run streamlit_app.py
```

Demo en linea: **[wyckoff-analysis-youngcanphoenix.streamlit.app](https://wyckoff-analysis-youngcanphoenix.streamlit.app/)**

## Herramientas

Arsenal del agente — 10 herramientas cuantitativas + 5 capacidades generales:

| Herramienta | Capacidad |
|-------------|-----------|
| `search_stock_by_name` | Busqueda difusa por nombre, codigo o pinyin |
| `analyze_stock` | Diagnostico Wyckoff / cotizaciones OHLCV recientes (cambio de modo) |
| `portfolio` | Ver posiciones / escaneo masivo de cartera (cambio de modo) |
| `update_portfolio` | Agregar / modificar / eliminar posiciones, establecer efectivo, eliminar registros de seguimiento |
| `get_market_overview` | Panorama general del mercado |
| `screen_stocks` | Filtrado del mercado completo con embudo de 5 capas (⚡segundo plano) |
| `generate_ai_report` | Reporte IA profundo en 3 campamentos (⚡segundo plano) |
| `generate_strategy_decision` | Decision de permanencia / entrada en posiciones (⚡segundo plano) |
| `query_history` | Historial de recomendaciones / pool de senales / registros de compra al cierre |
| `run_backtest` | Backtest historico de la estrategia de embudo (⚡segundo plano) |
| `check_background_tasks` | Consultar progreso de tareas en segundo plano |
| `exec_command` | Ejecutar comandos de shell locales |
| `read_file` | Leer archivos locales (CSV/Excel auto-parseados) |
| `write_file` | Escribir archivos (exportar reportes/datos) |
| `web_fetch` | Obtener contenido web (noticias financieras/anuncios) |

El orden y la frecuencia de las llamadas los decide el LLM en tiempo real, sin orquestacion previa. Envia una ruta CSV y lo lee; di "instala un paquete" y lo ejecuta.

## Embudo de 5 capas

| Capa | Nombre | Accion |
|------|--------|--------|
| L1 | Eliminar basura | Excluye ST / BSE / STAR Market; capitalizacion >= 3 500 M CNY; volumen diario >= 50 M CNY |
| L2 | Seleccion de 6 canales | Impulso / Ignicion / Latente / Acumulacion / Volumen minimo / Soporte |
| L3 | Resonancia sectorial | Filtro por distribucion Top-N de sectores |
| L4 | Micro-disparo | Cuatro senales clave: Spring / LPS / SOS / EVR |
| L5 | Juicio IA | Clasificacion LLM en 3 campamentos: Logica rota / Reserva / Plataforma de despegue |

## Automatizacion diaria

Tareas programadas con GitHub Actions integradas en el repositorio:

| Tarea | Hora (Beijing) | Descripcion |
|-------|---------------|-------------|
| Embudo + Reporte IA + Rebalanceo | Dom-Jue 18:25 | Totalmente automatico; resultados enviados a Feishu / Telegram |
| Estrategia de compra al cierre | Lun-Vie 13:50 | Puntuacion por reglas + revision LLM |
| Riesgo pre-mercado | Lun-Vie 08:20 | Alerta A50 + VIX |
| Resumen de limit-up | Lun-Vie 19:25 | Revision de acciones con alza diaria >= 8 % |
| Repricing de recomendaciones | Dom-Jue 23:00 | Sincroniza precios de cierre |
| Grid de backtest | 1 y 15 de cada mes 04:00 | 18 combos de parametros en paralelo → reporte agregado |
| Mantenimiento de cache | Diario 23:05 | Limpia cache de cotizaciones expiradas |

## Soporte de modelos

**CLI**: Gemini / Claude / OpenAI — cambia al instante con `/model`; compatible con cualquier endpoint compatible con OpenAI (DeepSeek / Qwen / Kimi, etc.).

**Web / Pipeline**: Gemini / OpenAI / DeepSeek / Qwen / Kimi / Zhipu / Volcengine / Minimax — 8 proveedores en total.

## Configuracion

**Zero configuracion para empezar** — solo lanza y `/model add` cualquier API Key de LLM. Los datos de cartera se guardan localmente por defecto.

Configuracion avanzada (archivo `.env` o GitHub Actions Secrets):

| Variable | Descripcion | Requerido? |
|----------|-------------|------------|
| LLM API Key | Configurar via `/model add` interactivamente | Si |
| `TUSHARE_TOKEN` | Datos del mercado bursatil (`/config set tushare_token`) | Si |
| `SUPABASE_URL` / `SUPABASE_KEY` | Sincronizacion en la nube (multi-dispositivo) | Opcional |
| `TICKFLOW_API_KEY` | TickFlow en tiempo real / intradias | Opcional |
| `FEISHU_WEBHOOK_URL` | Push Feishu | Opcional |
| `TG_BOT_TOKEN` + `TG_CHAT_ID` | Push Telegram | Opcional |

Consulta la configuracion completa y los secretos de GitHub Actions en la [documentacion de arquitectura](ARCHITECTURE.md).

## MCP Server

Expone las capacidades de analisis Wyckoff a traves del [protocolo MCP](https://modelcontextprotocol.io/), permitiendo a Claude Code / Cursor / cualquier cliente MCP llamar a 10 herramientas directamente.

```bash
# Instalar dependencia MCP
uv pip install youngcan-wyckoff-analysis[mcp]

# Registrar en Claude Code
claude mcp add wyckoff -- wyckoff-mcp
```

O agregar manualmente en la configuracion del cliente MCP:

```json
{
  "mcpServers": {
    "wyckoff": {
      "command": "wyckoff-mcp",
      "env": {
        "TUSHARE_TOKEN": "your_token",
        "TICKFLOW_API_KEY": "your_key"
      }
    }
  }
}
```

Una vez registrado, solo pregunta "diagnostica 000001" en Claude Code / Cursor para invocar las herramientas Wyckoff.

## Wyckoff Skills

Reutilizacion ligera de la capacidad de analisis Wyckoff: [`YoungCan-Wang/wyckoff_skill`](https://github.com/YoungCan-Wang/wyckoff_skill.git)

Ideal para dar a cualquier asistente IA una rapida "perspectiva Wyckoff."

## Aviso de riesgo

> **Esta herramienta identifica potencial basandose en patrones historicos de volumen y precio. El rendimiento pasado no garantiza resultados futuros. Todos los resultados de filtrado, recomendacion y backtesting no constituyen asesoramiento de inversion. Invierta bajo su propia responsabilidad.**

## Licencia

[AGPL-3.0](../LICENSE) &copy; 2024-2026 youngcan

---

[![Star History Chart](https://api.star-history.com/svg?repos=YoungCan-Wang/Wyckoff-Analysis&type=Date)](https://star-history.com/#YoungCan-Wang/Wyckoff-Analysis&Date)
