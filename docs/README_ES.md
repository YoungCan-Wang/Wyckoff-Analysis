<div align="center">

# Wyckoff Trading Agent

**Sistema cuantitativo Wyckoff para acciones A, Hong Kong y EE. UU. — hablas con naturalidad, el agente lee el mercado**

[![PyPI](https://img.shields.io/pypi/v/youngcan-wyckoff-analysis?color=blue)](https://pypi.org/project/youngcan-wyckoff-analysis/)
[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-AGPL--3.0-green.svg)](../LICENSE)
[![Web App](https://img.shields.io/badge/Web-React%20App-0ea5e9.svg)](https://wyckoff-analysis.pages.dev/)
[![Homepage](https://img.shields.io/badge/homepage-Wyckoff%20Homepage-0ea5e9.svg)](https://youngcan-wang.github.io/wyckoff-homepage/)

[中文](../README.md) | [English](README_EN.md) | [日本語](README_JA.md) | [한국어](README_KO.md) | [Arquitectura](ARCHITECTURE.md)

</div>

---

Conversa en lenguaje natural con un agente IA experto en el metodo Wyckoff. Despacha de forma autonoma 10 herramientas profesionales + 5 capacidades generales, encadena razonamientos de multiples pasos y te da conclusiones accionables de trading.

Web + CLI + MCP triple canal, compatible con Gemini / Claude / OpenAI / DeepSeek, automatizacion completa via GitHub Actions.

Pagina del proyecto: **[youngcan-wang.github.io/wyckoff-homepage](https://youngcan-wang.github.io/wyckoff-homepage/)**

> Aviso de riesgo: WyckoffAgent es una herramienta educativa, de investigacion e informacion. No ofrece asesoramiento de inversion, no cubre todas las circunstancias financieras personales y no garantiza resultados futuros.

## Navegacion de documentos

| Tema | Donde leer |
|---|---|
| Uso, despliegue, configuracion | Este README |
| Arquitectura, Actions, tablas de datos, cache | [ARCHITECTURE.md](ARCHITECTURE.md) |
| Embudo, reportes IA, OMS, backtesting | [../README_STRATEGY.md](../README_STRATEGY.md) |
| Glosario | [../GLOSSARY.md](../GLOSSARY.md) |
| Notas de investigacion y operaciones | [../wiki_repo_new/Home.md](../wiki_repo_new/Home.md) |

## Special Thanks

<table>
  <tr>
    <td width="150" align="center">
      <a href="https://tickflow.org/auth/register?ref=5N4NKTCPL4">
        <img src="../attach/tickflow-logo.png" alt="TickFlow" width="120" />
      </a>
    </td>
    <td>
      <strong><a href="https://tickflow.org/auth/register?ref=5N4NKTCPL4">TickFlow</a></strong><br />
      Gracias a TickFlow por proporcionar a WyckoffAgent datos de alta calidad para acciones A, EE. UU. y Hong Kong.
    </td>
  </tr>
</table>

## Uso online

No requiere instalacion.

**React Web App**: **[wyckoff-analysis.pages.dev](https://wyckoff-analysis.pages.dev/)**

SPA moderna con chat de AI Agent, gestion de cartera, screener de embudo, seguimiento de recomendaciones, exportacion de datos, streaming y visualizacion de llamadas a herramientas.

| Sala de chat | Screener de embudo |
|:---:|:---:|
| <img src="screenshots/web-chat.png" width="450" /> | <img src="screenshots/web-screen.png" width="450" /> |

| Seguimiento de recomendaciones | Cartera |
|:---:|:---:|
| <img src="screenshots/web-track.png" width="450" /> | <img src="screenshots/web-portfolio.png" width="450" /> |

**Portal Streamlit de mantenimiento**: **[wyckoff-analysis-youngcanphoenix.streamlit.app](https://wyckoff-analysis-youngcanphoenix.streamlit.app/)**

Streamlit sostuvo el MVP inicial y sigue disponible para exportacion de datos, analisis de acciones individuales y configuracion.

| Sala de chat | Exportacion de datos |
|:---:|:---:|
| <img src="../attach/web-chat.png" width="450" /> | <img src="../attach/web-export.png" width="450" /> |

## Funcionalidades

| Capacidad | Descripcion |
|-----------|-------------|
| Agente conversacional | Diagnosticos, filtros y reportes activados con lenguaje natural; el LLM orquesta herramientas de forma autonoma; tambien lee/escribe archivos, ejecuta comandos y obtiene contenido web |
| Skills | Comandos slash integrados (`/screen`, `/checkup`, `/report`, `/strategy`, `/backtest`) para flujos complejos con un toque; extensible por el usuario via `~/.wyckoff/skills/*.md` |
| Embudo de 5 capas | Escaneo completo de acciones A y universos independientes de Hong Kong / EE. UU. mediante seis canales + resonancia sectorial + micro-disparo |
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

> **Recomendado: conectar TickFlow para datos mas fuertes en tiempo real e intradia de acciones A / EE. UU. / Hong Kong**
> Registro: [Enlace de registro TickFlow](https://tickflow.org/auth/register?ref=5N4NKTCPL4)

## Uso local

### CLI — recomendado

Flujo nativo de terminal con el conjunto de funciones mas completo. Soporta tareas en segundo plano, memoria, Skills, MCP Server y almacenamiento local SQLite.

### Instalacion en una linea (recomendado)

```bash
curl -fsSL https://raw.githubusercontent.com/YoungCan-Wang/WyckoffTradingAgent/main/install.sh | bash
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

| Pantalla inicial | Consulta de cartera |
|:---:|:---:|
| <img src="../attach/cli-home.png" width="450" /> | <img src="../attach/cli-running.png" width="450" /> |

| Reporte de diagnostico | Instrucciones de operacion |
|:---:|:---:|
| <img src="../attach/cli-analysis.png" width="450" /> | <img src="../attach/cli-result.png" width="450" /> |

### Panel local

```bash
wyckoff dashboard
```

Inicia un panel HTTP local (puerto 8765 por defecto) y abre el navegador automaticamente. Todos los datos quedan en SQLite local.

Incluye recomendaciones, senales, cartera, memoria del agente, configuracion, logs de chat, logs del agente y estado de sincronizacion. Soporta tema oscuro/claro y UI bilingue CN/EN.

| Resumen | Logs de chat | Detalle de trace |
|:---:|:---:|:---:|
| <img src="../attach/dashboard-overview.png" width="300" /> | <img src="../attach/dashboard-chatlog.png" width="300" /> | <img src="../attach/dashboard-chatlog-trace.png" width="300" /> |

### Grid de backtest

18 combinaciones de parametros en paralelo, salida de parametros optimos, matriz Sharpe y revision de estrategia:

| Parametros optimos & Ranking | Matriz de parametros |
|:---:|:---:|
| <img src="../attach/backtest-grid-1.png" width="450" /> | <img src="../attach/backtest-grid-2.png" width="450" /> |

### Web local

React SPA local, compartiendo la misma base SQLite local con CLI:

```bash
cd web/apps/web
pnpm install
pnpm dev
```

Ejecucion local legacy de Streamlit:

```bash
git clone https://github.com/YoungCan-Wang/WyckoffTradingAgent.git
cd WyckoffTradingAgent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
streamlit run streamlit_app.py
```

Web App: **[wyckoff-analysis.pages.dev](https://wyckoff-analysis.pages.dev/)**

Streamlit: **[wyckoff-analysis-youngcanphoenix.streamlit.app](https://wyckoff-analysis-youngcanphoenix.streamlit.app/)**

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
| L4 | Micro-disparo | Cinco senales clave: Spring / LPS / SOS / EVR / Compression |
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
| Mantenimiento de base de datos | Diario 23:05 | Limpia cotizaciones, órdenes, señales y datos de mercado fuera de la ventana |

## Soporte de modelos

**CLI**: Gemini / Claude / OpenAI — cambia al instante con `/model`; compatible con cualquier endpoint compatible con OpenAI (DeepSeek / Qwen / Kimi, etc.).

**Web / Pipeline**: 1Route / Gemini / OpenAI / Zhipu / Minimax / DeepSeek / Qwen / Volcengine. Kimi y otros proveedores compatibles con OpenAI pueden configurarse con `base_url` / `custom_providers`.

## Configuracion

**Cero configuracion para empezar** — solo lanza y `/model add` cualquier API Key de LLM. Los datos de cartera se guardan localmente por defecto.

Configuracion avanzada (archivo `.env` o GitHub Actions Secrets):

| Variable | Descripcion | Requerido? |
|----------|-------------|------------|
| LLM API Key | Configurar via `/model add` interactivamente | Si |
| `TUSHARE_TOKEN` | Datos del mercado bursatil (`/config set tushare_token`) | Si |
| `SUPABASE_URL` / `SUPABASE_KEY` | Sincronizacion en la nube (multi-dispositivo) | Opcional |
| `TICKFLOW_API_KEY` | TickFlow en tiempo real / intradias | Opcional |
| `FEISHU_WEBHOOK_URL` | Push Feishu | Opcional |
| `TG_BOT_TOKEN` + `TG_CHAT_ID` | Push Telegram | Opcional |

> Fuente de datos: [TickFlow →](https://tickflow.org/auth/register?ref=5N4NKTCPL4) | API LLM: [1Route →](https://www.1route.dev/register?aff=359904261)

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

## Comunidad

| Grupo Feishu | Grupo QQ | Contacto Feishu |
|:---:|:---:|:---:|
| <img src="../attach/飞书群二维码.png" width="200" /> | <img src="../attach/QQ群二维码.jpg" width="200" /><br/>Grupo: 761348919 | <img src="../attach/飞书个人二维码.png" width="200" /> |

## Patrocinio

Si el proyecto te ayuda, una Star se agradece. Si te ayuda a ganar dinero, invita al autor a una hamburguesa.

| Alipay | WeChat |
|:---:|:---:|
| <img src="../attach/支付宝收款码.jpg" width="200" /> | <img src="../attach/微信收款码.png" width="200" /> |

## Aviso de riesgo

> **Esta herramienta identifica potencial basandose en patrones historicos de volumen y precio. El rendimiento pasado no garantiza resultados futuros. Todos los resultados de filtrado, recomendacion y backtesting no constituyen asesoramiento de inversion. Invierta bajo su propia responsabilidad.**

## Licencia

[AGPL-3.0](../LICENSE) &copy; 2024-2026 youngcan

---

[![Star History Chart](https://api.star-history.com/svg?repos=YoungCan-Wang/WyckoffTradingAgent&type=Date)](https://star-history.com/#YoungCan-Wang/WyckoffTradingAgent&Date)
