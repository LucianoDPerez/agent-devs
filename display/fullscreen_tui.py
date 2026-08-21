"""TUI full-screen estilo opencode (--tui) sobre Textual.

┌──────────────────────────────────────┐
│  RichLog (mensajes, scroll+rueda)    │  ← todo el output, con color
├──────────────────────────────────────┤
│  TextArea (input, scroll interno)    │  ← caja abajo
│  Static toolbar (dock bottom)        │  ← SIEMPRE fija, última línea
└──────────────────────────────────────┘

Textual (App full-screen real) resuelve de fábrica lo que el prototipo de
prompt_toolkit peleaba: layout fijo a la terminal, scroll de rueda/mouse sobre
el panel, auto-follow, y resize estable. La captura de output es
console.file → PaneWriter (solo acumula) → pull del hilo de UI (10 Hz) →
Text.from_ansi. ESC cancela el turno; Enter envía; ⌥+Enter salto; Ctrl+C sale.
"""
from __future__ import annotations

import subprocess
import threading
import traceback

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Static, TextArea

from llm_wrapper import get_usage
from display.console import MD_BEGIN, MD_END

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_MAX_PANE_CHARS = 200_000   # tope de memoria del pane (trims desde el head)


class PaneWriter:
    """File-like que captura console.file / sys.stdout / sys.stderr.

    SOLO ACUMULA (thread-safe). La UI CONSUME con pull() desde un timer en el
    hilo de UI: sin call_from_thread bloqueante ni entregas worker→UI. El
    esquema viejo tenía carreras entre write() y el timer de flush() (el cb se
    invocaba fuera del lock): se perdían los \\n de frontera entre writes
    (síntoma: tool calls y filas de tablas markdown pegadas en una línea).

    isatty()=True → rich emite ANSI. Pre-mount los writes van a _pending y el
    mount los vuelca al pane en orden.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self._attached = False
        self._pending: list[str] = []   # writes previos al mount
        self._buf = ""                  # acumulado sin consumir (\n intactos)

    def write(self, text: str) -> None:
        if not text:
            return
        with self.lock:
            if not self._attached:
                self._pending.append(text)
            else:
                self._buf += text

    def flush(self) -> None:
        # rich lo llama tras escribir; con el modelo pull no hay nada que
        # descargar acá: la UI consume por su cuenta.
        pass

    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        # rich consulta fileno para leer el tamaño real de la terminal; sin
        # esto cae a 80 columnas y envuelve el texto ANTES de llegar al panel
        # (síntoma: respuestas del LLM en una columna angosta). Devolvemos el
        # fd real de la tty para que rich use el ancho actual.
        try:
            import sys as _sys
            return _sys.__stdout__.fileno()
        except Exception:
            return 1

    def attach(self) -> list[str]:
        """El mount toma ownership: devuelve los writes pre-mount (en orden)
        para volcarlos al pane y habilita el buffering normal."""
        with self.lock:
            pre, self._pending = self._pending, []
            self._attached = True
        return pre

    def pull(self) -> str:
        """UI: consume lo acumulado hasta ahora (no bloquea, preserva orden)."""
        with self.lock:
            buf, self._buf = self._buf, ""
        return buf


class SelectablePane(Static):
    """Static del pane con get_selection QUE FUNCIONA.

    El Static heredado extrae de self._render() — que acá devuelve un
    RichVisual, no un Text → Screen.get_selected_text() devolvía None y el
    copiado jamás funcionaba. Esta subclase extrae directamente del acumulado
    de la App (self.app._pane_text.plain) mapeando los offsets de la selección
    a posiciones del texto plano (mismo orden: la selección nace del visual
    del pane, que ES ese Text).
    """

    def get_selection(self, selection) -> tuple[str, str] | None:
        """Extrae el texto seleccionado del acumulado de la App.

        El Static heredado extrae de self._render() — que acá devuelve un
        RichVisual, no un Text → Screen.get_selected_text() daba None y el
        copiado nunca funcionaba. Esta subclase extrae del plain acumulado
        con selection.extract() (la utilidad oficial de Textual para mapear
        offsets de selección a substrings).
        """
        try:
            plain = self.app._pane_text.plain
        except Exception:
            return None
        if not plain:
            return None
        try:
            return selection.extract(plain), "\n"
        except Exception:
            return None

    def selection_updated(self, selection) -> None:
        """Cachea el texto en el drag-selection.

        Al presionar ⌘C/Ctrl+C el keystroke limpia la selección ANTES de la
        acción (get_selected_text() da None en ese instante) — con este
        cache, action_copy_or_quit puede usar el texto que YA se capturó
        durante el drag. E2E real: click derecho copiaba, ⌘C no.
        """
        try:
            super().selection_updated(selection)
        except Exception:
            pass
        if selection is None:
            # no sobreescribir el cache con None (se limpia por keystroke)
            return
        try:
            plain = self.app._pane_text.plain
            if plain:
                extracted = selection.extract(plain)
                if extracted:
                    self.app._last_selected_text = extracted
        except Exception:
            pass

    def on_mouse_down(self, event) -> None:
        """Click DERECHO sobre lo seleccionado → copiar al portapapeles.

        SIEMPRE atrapa excepciones (una excepción de un click no debe poder
        cerrar la app — E2E real: click aparentemente random cerraba la TUI).
        """
        try:
            if event.button == 3:   # botón derecho
                try:
                    sel = self.app.screen.get_selected_text()
                except Exception:
                    sel = ""
                if sel:
                    try:
                        import subprocess
                        subprocess.run(["pbcopy"], input=sel.encode("utf-8"),
                                       check=True, timeout=3)
                    except Exception:
                        pass
                    try:
                        self.app.notify("Copiado", severity="information")
                    except Exception:
                        pass
        except Exception:
            pass
        try:
            return super().on_mouse_down(event)
        except Exception:
            return None


class FullscreenTUI(App):
    CSS = """
    #header {
        dock: top;
        height: auto;
        background: #0f0f1a;
        padding: 0 1;
    }
    #pane {
        height: 1fr;
        background: #121212;
        border: none;
        padding: 0 1;
    }
    #pane_text {
        width: 100%;
        background: #121212;
    }
    #input {
        height: 6;
        background: #2a2a3e;
        color: #e0e0e0;
        border-top: solid #4a4a6e;
        padding: 0 1;
    }
    #toolbar {
        dock: bottom;
        height: 1;
        background: #1a1a2e;
        color: #e0e0e0;
    }
    """
    BINDINGS = [
        Binding("enter", "submit", priority=True),
        Binding("escape", "cancel_turn", priority=True),
        Binding("ctrl+c", "copy_or_quit", priority=True),
        Binding("alt+enter", "input_newline"),
        Binding("ctrl+shift+c", "copy_selection"),
        Binding("ctrl+y", "copy_selection"),
    ]

    def __init__(self, status_provider, on_submit):
        super().__init__()
        self.status_provider = status_provider
        self.on_submit = on_submit
        self.on_cancel = None
        self.pane_writer = PaneWriter()
        self._busy = False
        self._pane_text: Text = Text()   # acumulado único del pane
        self._exit_armed = False         # confirmación de salida (ctl+c ×2)
        self._last_selected_text = ""    # cache del drag-selection (⌘C)
        # Modelo real cargado en llama-server: se detecta lazy al primer
        # render del header (fallback a LLM_MODEL_NAME si no hay server).
        self._model_name: str | None = None
        # Render de markdown por segmento (marcadores de display.console)
        self._md_active = False
        self._md_raw = ""
        self._md_start = 0

    # ── UI ─────────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        yield Static(id="header")
        yield VerticalScroll(
            SelectablePane(id="pane_text", markup=False),
            id="pane",
        )
        yield Static(id="toolbar")
        yield TextArea(id="input", text="")

    def on_mount(self) -> None:
        self.query_one("#header", Static).update(self._build_header())
        for chunk in self.pane_writer.attach():
            self._append_pane(chunk)
        self.query_one("#input", TextArea).focus()
        self.set_interval(0.25, self._refresh_toolbar)
        # Pull del pane DESDE EL HILO DE UI: consume lo acumulado por el
        # worker. 10 Hz = latencia imperceptible y orden garantizado; los \n
        # viajan dentro del buffer y jamás se recortan en fronteras de write.
        self.set_interval(0.1, self._pull_pane)

    def _pull_pane(self) -> None:
        data = self.pane_writer.pull()
        if data:
            self._append_pane(data)

    def _build_header(self) -> Text:
        """Header FIJO arriba (estilo opencode): info del entorno del harness.

        No scrollea: queda clavado en el tope; solo el RichLog del medio
        scrollea. Toma los datos de status_provider() (repo/branch/tools) +
        config del LLM.
        """
        from config import (
            LLM_BASE_URL, LLM_MAX_TOKENS, LLM_MODEL_NAME, LLM_TEMPERATURE,
        )
        from llm_wrapper import detect_server_model

        if self._model_name is None:
            # Modelo REAL cargado en llama-server (el config puede quedar
            # viejo si el usuario carga otro modelo). Fail-open a config.
            self._model_name = detect_server_model(LLM_BASE_URL) or LLM_MODEL_NAME
        try:
            st = self.status_provider() or {}
        except Exception:
            st = {}
        repo = (st.get("repo") or "-").rstrip("/").split("/")[-1]
        branch = st.get("branch", "-")
        tools = st.get("tools", "?")
        lines = [
            (" 🔌 LLM: ", "cyan bold"), (LLM_BASE_URL, ""),
            ("   📁 ", "cyan bold"), (repo, ""), ("  🌿 ", "green bold"), (branch, ""),
        ]
        right = [
            (" ⚡ ", "yellow bold"), (self._model_name, ""),
            (" | Temp: ", "dim"), (f"{LLM_TEMPERATURE}", ""),
            (" | Max: ", "dim"), (f"{LLM_MAX_TOKENS}", ""),
            (" | 🛠️  ", "yellow bold"), (f"{tools}", ""),
        ]
        from rich.panel import Panel
        inner = Text.assemble(*lines)
        inner.append("\n")
        inner += Text.assemble(*right)
        return Panel(
            inner,
            title=" AgentDevs ",
            title_align="left",
            border_style="blue",
            padding=(0, 1),
            # expand=True: el header ocupa TODO el ancho de la terminal,
            # igual que toolbar e input (sin esto queda del ancho del texto).
            expand=True,
        )

    # ── output: append al pane (SIEMPRE en hilo de UI, vía _pull_pane) ─
    def _append_pane(self, raw: str) -> None:
        """Append del streaming a UN ÚNICO Text acumulado (no entradas).

        Además procesa los marcadores MD_BEGIN/MD_END de display.console:
        el tramo entre marcadores es markdown CRUDO del LLM; al llegar MD_END
        se reemplaza por la versión renderizada (rich Markdown) vía splice.
        Mientras llega, se muestra crudo (streaming honesto).
        """
        while raw:
            if self._md_active:
                end_idx = raw.find(MD_END)
                if end_idx == -1:
                    self._md_raw += raw
                    self._append_plain(raw)
                    return
                self._md_raw += raw[:end_idx]
                self._append_plain(raw[:end_idx])
                self._flush_markdown()
                raw = raw[end_idx + len(MD_END):]
                continue
            begin_idx = raw.find(MD_BEGIN)
            if begin_idx == -1:
                self._append_plain(raw)
                return
            self._append_plain(raw[:begin_idx])
            self._md_active = True
            self._md_raw = ""
            self._md_start = len(self._pane_text.plain)
            raw = raw[begin_idx + len(MD_BEGIN):]

    def _append_plain(self, raw: str) -> None:
        """Acumula texto tal cual (con ANSI → estilos rich) y refresca."""
        if not raw:
            return
        try:
            frag = Text.from_ansi(raw)
        except Exception:
            frag = Text(raw, style="dim")
        self._pane_text.append_text(frag)
        self._trim_and_update()

    def _flush_markdown(self) -> None:
        """Reemplaza el tramo crudo [MD_BEGIN..MD_END] por el renderizado."""
        self._md_active = False
        try:
            rendered = self._render_markdown(self._md_raw)
        except Exception:
            return  # fail-open: queda el crudo, que ya es legible
        plain = self._pane_text.plain
        idx = plain.find(self._md_raw, max(0, self._md_start - 2000))
        if idx == -1:
            return  # trim/edge: mejor dejar el crudo que corromper el pane
        pre = self._pane_text[:idx]
        post = self._pane_text[idx + len(self._md_raw):]
        nuevo = Text()
        nuevo.append_text(pre)
        nuevo.append_text(rendered)
        nuevo.append_text(post)
        self._pane_text = nuevo
        self._trim_and_update()

    def _render_markdown(self, md: str) -> Text:
        import io

        from rich.console import Console as RichConsole
        from rich.markdown import Markdown

        width = max(30, (self.size.width or 100) - 2)
        buf = io.StringIO()
        c = RichConsole(file=buf, force_terminal=True, width=width)
        c.print(Markdown(md, code_theme="monokai"))
        return Text.from_ansi(buf.getvalue())

    def _trim_and_update(self) -> None:
        if len(self._pane_text.plain) > _MAX_PANE_CHARS:
            excess = len(self._pane_text.plain) - _MAX_PANE_CHARS
            self._pane_text = Text(self._pane_text.plain[excess:], style="dim")
        self.query_one("#pane_text", Static).update(self._pane_text)
        # auto-follow: si estamos pegados al fondo, scrollear
        try:
            self.query_one("#pane", VerticalScroll).scroll_end(animate=False)
        except Exception:
            pass

    # ── toolbar ────────────────────────────────────────────────────────
    def _refresh_toolbar(self) -> None:
        try:
            st = self.status_provider() or {}
        except Exception:
            st = {}
        usage = get_usage()
        total = usage["session"]["prompt"] + usage["session"]["completion"]
        repo = (st.get("repo") or "-").rstrip("/").split("/")[-1]
        spin = _SPINNER[int(__import__("time").monotonic() * 4) % len(_SPINNER)]
        busy = f" {spin} trabajando…" if self._busy else ""
        txt = Text.assemble(
            (" 🌿 " + str(st.get("branch", "-")), "green"),
            ("  ·  ", "dim"),
            (f"⚡ {total:,} tokens", "yellow"),
            ("  ·  ", "dim"),
            (" " + str(st.get("role", "🔍")), "cyan"),
            ("  ·  ", "dim"),
            (f" 📁 {repo}", "blue"),
            (busy, "magenta"),
            ("   Enter=envía · ⌥Enter=salto · rueda=scroll · "
             "Selección+⌘C/⌃Y=copiar · ESC=cancela · Ctrl+C=salir", "bright_black"),
        )
        self.query_one("#toolbar", Static).update(txt)

    # ── acciones ───────────────────────────────────────────────────────
    def action_submit(self) -> None:
        ta = self.query_one("#input", TextArea)
        text = ta.text
        if not text.strip():
            return
        ta.clear()
        self._busy = True
        self._refresh_toolbar()
        threading.Thread(target=self._safe_submit, args=(text,), daemon=True).start()

    def action_quit_app(self) -> None:
        self.exit()

    def _copy_to_system(self, text: str) -> None:
        """Vuelca texto al portapapeles del SISTEMA (pbcopy en macOS)."""
        try:
            subprocess.run(["pbcopy"], input=text.encode("utf-8"),
                           check=True, timeout=3)
        except Exception:
            pass

    def action_copy_or_quit(self) -> None:
        """Ctrl+C / ⌘C: copia si hay selección; si no, sale CON CONFIRMACIÓN.

        En macOS ⌘C llega como Ctrl+C para la app. Antes un Ctrl+C sin
        selección salía de inmediato — un click/tecla accidental podía
        cerrar la TUI. Ahora: con selección copia; sin selección ARMA la
        salida (un segundo Ctrl+C confirma; ESC/Segundo Ctrl+C... solo el
        segundo Ctrl+C cierra).
        """
        sel = ""
        try:
            sel = self.screen.get_selected_text() or ""
        except Exception:
            sel = ""
        if not sel:
            # fallback: el cache del drag-selection (el keystroke limpia la
            # selección antes de la acción, pero el texto quedó capturado)
            sel = self._last_selected_text
        if sel.strip():
            self._copy_to_system(sel)
            self._exit_armed = False
            return
        if self._exit_armed:
            self.exit()
            return
        self._exit_armed = True
        try:
            self.notify("Ctrl+C de nuevo para salir", severity="warning")
        except Exception:
            pass

    def action_cancel_turn(self) -> None:
        if self.on_cancel is not None:
            try:
                self.on_cancel()
            except Exception:
                pass
        self._exit_armed = False

    def action_copy_selection(self) -> None:
        """Copia la selección del panel al portapapeles (sistema + Textual).

        Textual gestiona la selección del panel (mouse arrastrando / shift);
        action_copy_text() la pasa al clipboard de la app, y acá además la
        volcamos a pbcopy para que sea pegable en cualquier app.
        """
        try:
            self.screen.action_copy_text()
        except Exception:
            pass
        txt = self.clipboard
        if txt:
            self._copy_to_system(txt)

    def action_input_newline(self) -> None:
        self.query_one("#input", TextArea).insert("\n")

    def _safe_submit(self, text: str) -> None:
        try:
            self.on_submit(text)
        except Exception:
            self.pane_writer.write(traceback.format_exc())
        finally:
            self._busy = False
            try:
                self.call_from_thread(self._refresh_toolbar)
            except Exception:
                pass

    # ── API ────────────────────────────────────────────────────────────
    def write(self, text: str) -> None:
        """Output del harness → panel (thread-safe)."""
        self.pane_writer.write(text)
