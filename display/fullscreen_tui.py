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

import contextlib
import subprocess
import sys
import threading
import traceback

from rich.style import Style
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.strip import Strip
from textual.widgets import RichLog, Static, TextArea

from display.console import MD_BEGIN, MD_END, harness_head


def _is_approval_text(text: str) -> bool:
    """True si el texto parece respuesta sí/no a una confirmación pendiente."""
    from core.textutil import normalize

    return normalize(text) in ("s", "si", "y", "yes", "n", "no")

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_MD_MAX_RENDER_CHARS = 8_000  # markdown mayor que esto queda crudo (no render)

_SEL_LOG = "/tmp/agentdevs-selection.log"  # telemetria drag/copy (se rota a 200KB)


def _sel_log(msg: str) -> None:
    """Telemetria de seleccion a archivo (barata: solo en eventos de drag/copy)."""
    try:
        import os
        import time
        try:
            if os.path.getsize(_SEL_LOG) > 200_000:
                os.replace(_SEL_LOG, _SEL_LOG + ".old")
        except OSError:
            pass
        with open(_SEL_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass


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


class SelectablePane(RichLog):
    """RichLog virtualizado con copia QUE FUNCIONA.

    Antes era un Static con UN ÚNICO Text de 200k chars: cada append
    re-layouteaba TODO (scroll/selección tediosos con mucho texto).
    RichLog virtualiza por línea: solo renderiza lo visible y descarta
    lo viejo con max_lines (sin copias O(n) ni splices).

    Selección: el RichLog upstream no trae offsets de selección (su
    get_selection heredado devuelve None y el drag no pinta nada).
    Acá se agregan como TextLog: render_line aplica offsets + hornea
    el highlight del span, y get_selection extrae de las propias
    líneas del log (alineación 1:1 por construcción).
    """

    def render_line(self, y: int) -> Strip:
        """Fila visible con highlight horneado + offsets de selección.

        Orden estricto (como TextLog): PRIMERO se parte/estila, ÚLTIMO
        se aplican offsets. Hacerlo al revés deja metas viejas en los
        sub-segmentos y el mapeo del drag se corre (el end caía corto).
        """
        scroll_x, scroll_y = self.scroll_offset
        try:
            strip = super().render_line(y)
        except Exception:
            return Strip.blank(self.size.width if self.size else 80)
        try:
            selection = self.text_selection
        except Exception:
            selection = None
        if selection is not None:
            try:
                span = selection.get_span(scroll_y + y)
            except Exception:
                span = None
            if span is not None:
                start, end = span
                try:
                    styled = self._stylize_span(strip, start, end)
                    if styled is not None:
                        strip = styled
                except Exception:
                    pass
        try:
            strip = strip.apply_offsets(scroll_x, scroll_y + y)
        except Exception:
            pass
        return strip

    @staticmethod
    def _selection_style(app) -> Style | None:
        """Estilo de selección con contraste GARANTIZADO.

        El estilo del tema resolvía a fg == bg (#094472 on #094472):
        bloque sólido que tapaba el texto. Acá se fuerza frente
        blanco brillante + bold sobre el fondo del tema.
        (Ojo: los nombres "ansi_*" NO existen en este Rich — solo
        "blue"/"bright_white" — y .name de un Color puede dar None:
        cualquier fallo cae a reverse, nunca a invisible.)
        """
        from rich.style import Style as RichStyle

        try:
            comp = app.screen.get_component_styles("screen--selection")
            bg = comp.background
        except Exception:
            bg = None
        for kwargs in (
            {"color": "bright_white", "bgcolor": bg, "bold": True},
            {"color": "bright_white", "bgcolor": "blue", "bold": True},
            {"reverse": True, "bold": True},
        ):
            try:
                if kwargs.get("bgcolor") is None:
                    continue
                return RichStyle(**kwargs)
            except Exception:
                continue
        return None

    def _stylize_span(self, strip: Strip, start: int, end: int) -> Strip | None:
        """Aplica el estilo de selección a [start, end) sin tocar el texto."""
        from rich.segment import Segment

        try:
            text = strip.text
            if end == -1 or end > len(text):
                end = len(text)
            if start < 0:
                start = 0
            if start >= end:
                return None
            style = self._selection_style(self.app)
            if style is None:
                return None
            out: list[Segment] = []
            x = 0
            for seg in strip:
                seg_text = seg.text or ""
                seg_end = x + len(seg_text)
                if seg_end <= start or x >= end:
                    out.append(seg)
                else:
                    a = max(start - x, 0)
                    b = min(end - x, len(seg_text))
                    base = seg.style
                    try:
                        control = seg.control
                    except Exception:
                        control = None
                    if a > 0:
                        out.append(Segment(seg_text[:a], base, control))
                    try:
                        combined = base + style if base is not None else style
                    except Exception:
                        combined = style
                    out.append(Segment(seg_text[a:b], combined, control))
                    if b < len(seg_text):
                        out.append(Segment(seg_text[b:], base, control))
                x = seg_end
            try:
                cell_length = strip.cell_length
            except Exception:
                cell_length = sum(len(s.text or "") for s in out)
            return Strip(out, cell_length)
        except Exception:
            return None

    def get_selection(self, selection) -> tuple[str, str] | None:
        """Extrae de las propias líneas del log (alineación exacta)."""
        try:
            text = "\n".join(line.text for line in self.lines)
        except Exception:
            return None
        if not text.strip():
            return None
        try:
            out = selection.extract(text), "\n"
            _sel_log(f"get_selection sel={selection!r} nlines={len(self.lines)} out_len={len(out[0])}")
            return out
        except Exception as e:
            _sel_log(f"get_selection ERROR {e!r}")
            return None

    def selection_updated(self, selection) -> None:
        """Cachea el texto + invalida highlight (el _line_cache del
        RichLog no contempla selección y dejaría el pintado rancio)."""
        try:
            self._line_cache.clear()
        except Exception:
            pass
        with contextlib.suppress(Exception):
            super().selection_updated(selection)
        with contextlib.suppress(Exception):
            self.refresh()
        _sel_log(f"selection_updated sel={selection!r}")
        if selection is None:
            # no sobreescribir el cache con None (se limpia por keystroke)
            return
        try:
            sel = self.app.screen.get_selected_text()
            if sel and sel.strip():
                self.app._last_selected_text = sel
                _sel_log(f"cache len={len(sel)}")
        except Exception:
            pass

    def on_mouse_down(self, event) -> None:
        """Click DERECHO sobre lo seleccionado → copiar al portapapeles.

        SIEMPRE atrapa excepciones (una excepción de un click no debe poder
        cerrar la app — E2E real: click aparentemente random cerraba la TUI).
        """
        _sel_log(f"mouse_down button={event.button} pos=({event.x},{event.y})")
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
                    with contextlib.suppress(Exception):
                        self.app.notify("Copiado", severity="information")
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
    #slash-hint {
        height: auto;
        background: #1a1a2e;
        color: #9a9ab5;
        padding: 0 1;
        display: none;
    }
    """
    BINDINGS = [
        Binding("enter", "submit", priority=True),
        Binding("escape", "cancel_turn", priority=True),
        # Tab completa el comando sugerido (solo con la tira visible; si no,
        # indenta como siempre — no cambia la conducta normal).
        Binding("tab", "slash_complete", priority=True),
        # Flechas navegan la tira de slash commands (solo visible; si no,
        # check_action las deshabilita y el cursor se mueve normal).
        Binding("up", "slash_up", priority=True),
        Binding("down", "slash_down", priority=True),
        Binding("ctrl+c", "copy_or_quit", priority=True),
        Binding("alt+enter", "input_newline"),
        Binding("ctrl+shift+c", "copy_selection"),
        Binding("ctrl+y", "copy_selection"),
        # macOS: Cmd+C lo intercepta la terminal (copia del sistema, nunca
        # llega a la app) — el atajo de copia REAL en terminal es Option+C
        # (alt+c), que SÍ se transmite. Cmd+C igual sirve: la terminal copia
        # lo seleccionado visible.
        Binding("alt+c", "copy_selection"),
    ]

    def __init__(self, status_provider, on_submit):
        super().__init__()
        self.status_provider = status_provider
        self.on_submit = on_submit
        self.on_cancel = None
        self.pane_writer = PaneWriter()
        self._busy_count = 0
        self._busy_lock = threading.Lock()
        self._exit_armed = False         # confirmación de salida (ctl+c ×2)
        self._last_selected_text = ""    # cache del drag-selection (⌘C)
        # Modelo real cargado en llama-server: se detecta lazy al primer
        # render del header (fallback a LLM_MODEL_NAME si no hay server).
        self._model_name: str | None = None
        # Firma del header (base/modelo/repo/branch): si cambia, se reconstruye.
        self._header_sig: tuple | None = None
        # Render de markdown por segmento (marcadores de display.console)
        self._md_active = False
        self._md_raw = ""
        # Sugerencias de slash commands (tira sobre el input, estilo opencode)
        self._slash_matches: list[tuple[str, str]] = []
        self._slash_index = 0
        self._slash_visible = False

    # ── UI ─────────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        yield Static(id="header")
        yield SelectablePane(id="pane", wrap=True, markup=False,
                               max_lines=3000)
        yield Static(id="toolbar")
        yield Static(id="slash-hint")
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
        scrollea. Toma los datos de status_provider() (repo/branch/tools +
        LLM REAL de la sesión).
        """
        from config import LLM_BASE_URL

        try:
            st = self.status_provider() or {}
        except Exception:
            st = {}
        if self._model_name is None:
            # Placeholder hasta el primer status con modelo real.
            self._model_name = st.get("model") or "-"
        # El header sigue a la sesión (incluye /model en caliente).
        _live_model = st.get("model") or self._model_name
        _transport = st.get("transport") or "chat"
        _reasoning = st.get("reasoning")
        if _transport == "responses" and _reasoning:
            _live_model = f"{_live_model} [{_reasoning}]"
        elif _transport != "chat" and _transport:
            _live_model = f"{_live_model} ({_transport})"
        self._model_name = _live_model
        repo = (st.get("repo") or "-").rstrip("/").split("/")[-1]
        branch = st.get("branch", "-")
        _base_shown = st.get("llm_base") or LLM_BASE_URL
        lines = [
            (" 🔌 LLM: ", "cyan bold"), (_base_shown, ""),
            ("   📁 ", "cyan bold"), (repo, ""), ("  🌿 ", "green bold"), (branch, ""),
        ]
        right = [
            (" ⚡ ", "yellow bold"), (self._model_name, ""),
            (" 📌 ", "cyan"), (harness_head(), ""),
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
        """Append por LÍNEA al RichLog (virtualizado, sin re-layout total).

        Además procesa los marcadores MD_BEGIN/MD_END de display.console:
        el tramo markdown se BUFFEREA sin mostrar y al llegar MD_END se
        escribe ya renderizado (o crudo si es gigante). Sin splices O(n):
        el crudo nunca entra al log, así que no hay que reemplazar nada.
        """
        while raw:
            if self._md_active:
                end_idx = raw.find(MD_END)
                if end_idx == -1:
                    self._md_raw += raw
                    break
                self._md_raw += raw[:end_idx]
                self._flush_markdown()
                raw = raw[end_idx + len(MD_END):]
                continue
            begin_idx = raw.find(MD_BEGIN)
            if begin_idx == -1:
                self._write_lines(raw)
                break
            if begin_idx:
                self._write_lines(raw[:begin_idx])
            self._md_active = True
            self._md_raw = ""
            raw = raw[begin_idx + len(MD_BEGIN):]

    def _write_text_rows(self, frag: Text) -> None:
        """Escribe un Text al RichLog fila por fila (1 write = 1 fila).

        RichLog abre fila nueva por write: si se escribe el chunk tal cual,
        el "\n" final genera una tira vacia fantasma e infla el log al
        doble. Partiendo por "\n" (split preserva los spans ANSI) cada
        fila lógica queda en su propia tira.
        """
        try:
            pane = self.query_one("#pane", SelectablePane)
        except Exception:
            return
        try:
            at_end = pane.scroll_offset.y >= pane.max_scroll_y
        except Exception:
            at_end = True
        try:
            parts = frag.split("\n")
        except Exception:
            parts = [frag]
        while parts and not parts[-1].plain:
            parts.pop()
        if not parts:
            with contextlib.suppress(Exception):
                pane.write(Text(""), scroll_end=at_end)
            return
        for part in parts:
            with contextlib.suppress(Exception):
                pane.write(part, scroll_end=at_end)

    def _write_lines(self, raw: str) -> None:
        """Chunk crudo -> Text ANSI -> filas del RichLog."""
        if not raw:
            return
        try:
            frag = Text.from_ansi(raw)
        except Exception:
            frag = Text(raw, style="dim")
        self._write_text_rows(frag)

    def _flush_markdown(self) -> None:
        """Escribe el tramo markdown buffereado (renderizado o crudo)."""
        self._md_active = False
        raw, self._md_raw = self._md_raw, ""
        if not raw:
            return
        if len(raw) > _MD_MAX_RENDER_CHARS:
            # Tablas gigantes (veredictos de pool): renderarlas congela la
            # UI; el crudo ya es legible y seleccionable.
            self._write_lines(raw)
            return
        try:
            rendered = self._render_markdown(raw)
        except Exception:
            self._write_lines(raw)  # fail-open: crudo legible
            return
        self._write_text_rows(rendered)

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
        """Compat: el RichLog se auto-limita (max_lines) y auto-scrollea
        por write(scroll_end=). Se conserva el nombre por llamadas viejas."""
        return

    # ── toolbar ────────────────────────────────────────────────────────
    def _refresh_toolbar(self) -> None:
        try:
            st = self.status_provider() or {}
        except Exception:
            st = {}
        total = int(st.get("tokens") or 0)
        repo = (st.get("repo") or "-").rstrip("/").split("/")[-1]
        spin = _SPINNER[int(__import__("time").monotonic() * 4) % len(_SPINNER)]
        busy = f" {spin} trabajando…" if self._busy else ""
        # Confirmación de write pendiente (EXECUTE_CONFIRM_WRITES): el prompt
        # va al pane (scrollable y puede quedar fuera de vista) — el toolbar
        # y el placeholder del input dan el aviso visible SIEMPRE, para que el
        # usuario responda sí/no a tiempo y el turno no venza por no verlo.
        confirming = bool(st.get("confirm_pending"))
        with contextlib.suppress(Exception):
            self.query_one("#input", TextArea).placeholder = (
                "❓ Escribí sí / no y Enter para aprobar o rechazar"
                if confirming
                else ""
            )
        # Header vivo: /model, branch o repo pueden cambiar en caliente y el
        # header se construía una sola vez en on_mount (quedaba clavado en
        # el modelo anterior). Se reconstruye solo si cambió la firma.
        try:
            sig = (
                st.get("llm_base"), st.get("model"), st.get("transport"),
                st.get("reasoning"), st.get("repo"), st.get("branch"),
            )
            if sig != self._header_sig:
                self._header_sig = sig
                with contextlib.suppress(Exception):
                    self.query_one("#header", Static).update(self._build_header())
        except Exception:
            pass
        txt = Text.assemble(
            (" 🌿 " + str(st.get("branch", "-")), "green"),
            ((" ✅ auto", "bold black on yellow") if st.get("auto_approve") else ("", "")),
            ("  ·  ", "dim"),
            (f"⚡ {total:,} tok (API)", "dim"),
            ("  ·  ", "dim"),
            (" " + str(st.get("role", "🔍")), "cyan"),
            ("  ·  ", "dim"),
            (f" 📁 {repo}", "blue"),
            (busy, "magenta"),
            ("   Enter=envía · ⌥Enter=salto · rueda=scroll · "
             + ("Selección+⌥C=copiar · " if sys.platform == "darwin" else "Selección+Ctrl+Shift+C=copiar · ")
             + "ESC=cancela · Ctrl+C=salir", "bright_black"),
        )
        if confirming:
            txt = Text(
                " ⚠️  CONFIRMACIÓN PENDIENTE — respondé sí / no en el input  ",
                style="bold black on yellow",
            ) + txt
        self.query_one("#toolbar", Static).update(txt)

    # ── acciones ───────────────────────────────────────────────────────
    @property
    def _busy(self) -> bool:
        """Hay trabajo en curso (uno o más submits sin terminar).

        Contador en vez de booleano: un slash instantáneo (/autoapprove)
        que termina mientras un turno sigue corriendo no debe apagar el
        indicador "trabajando…" (el finally del slash lo ponía en False
        aunque el LLM seguía trabajando).
        """
        with self._busy_lock:
            return self._busy_count > 0

    def _mark_busy(self) -> None:
        with self._busy_lock:
            self._busy_count += 1

    def _mark_idle(self) -> None:
        with self._busy_lock:
            self._busy_count = max(0, self._busy_count - 1)

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        """Flechas ↑/↓ solo navegan con la tira visible (si no, cursor normal
        del TextArea). El resto de acciones siempre habilitado."""
        if action in ("slash_up", "slash_down"):
            try:
                return bool(self._slash_visible and self._slash_matches)
            except Exception:
                return False
        return True

    def action_slash_up(self) -> None:
        """↑: sugerencia anterior (wrap-around)."""
        if not self._slash_matches:
            return
        self._slash_index = (self._slash_index - 1) % len(self._slash_matches)
        self._render_slash_hint()

    def action_slash_down(self) -> None:
        """↓: sugerencia siguiente (wrap-around)."""
        if not self._slash_matches:
            return
        self._slash_index = (self._slash_index + 1) % len(self._slash_matches)
        self._render_slash_hint()

    def action_submit(self) -> None:
        ta = self.query_one("#input", TextArea)
        # Tira visible: Enter ejecuta la sugerencia SELECCIONADA (↑/↓).
        # Con argumentos → la completa y espera que los escribas; sin
        # argumentos → se ejecuta directo.
        if self._slash_visible and self._slash_matches:
            from display.commands import takes_args

            sel = self._slash_matches[self._slash_index][0]
            if takes_args(sel):
                ta.load_text(sel + " ")
                try:
                    doc = ta.document
                    last = doc.line_count - 1
                    ta.move_cursor((last, len(doc.get_line(last))))
                except Exception:
                    pass
                self._hide_slash_hint()
                return
            ta.load_text(sel)
            self._hide_slash_hint()
            text = sel
        else:
            text = ta.text
        if not text.strip():
            return
        if self._busy and not text.strip().startswith("/"):
            # Turno en curso: no pisarlo (dos run_turn mezclan estado y el ESC
            # pierde al turno viejo). Se preserva el input. Slash y respuestas
            # a confirmaciones ("sí"/"no") pasan igual: on_submit los rutea sin
            # abrir turno nuevo. El s/n también pasa si la confirmación está
            # por llegar (flag inminente): sin esto el s/n se descartaba en la
            # ventana donde el turno ya figura busy pero el evento aún no
            # existe (carrera s/n).
            try:
                st = self.status_provider() or {}
            except Exception:
                st = {}
            confirming = bool(st.get("confirm_pending"))
            imminent = bool(st.get("confirm_imminent"))
            approval = _is_approval_text(text)
            if not confirming and not (approval and imminent):
                if approval:
                    self.notify(
                        "Todavía no hay pedido de confirmación — esperá el "
                        "cartel ❓ y reintentá",
                        severity="warning",
                    )
                else:
                    self.notify(
                        "Hay un turno en curso — esperá o cancelalo con ESC",
                        severity="warning",
                    )
                return
        ta.clear()
        self._mark_busy()
        self._refresh_toolbar()
        threading.Thread(target=self._safe_submit, args=(text,), daemon=True).start()

    def action_quit_app(self) -> None:
        self.exit()

    def _copy_to_system(self, text: str) -> None:
        """Vuelca texto al portapapeles del SISTEMA (pbcopy en macOS)."""
        with contextlib.suppress(Exception):
            subprocess.run(["pbcopy"], input=text.encode("utf-8"),
                           check=True, timeout=3)

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
        with contextlib.suppress(Exception):
            self.notify("Ctrl+C de nuevo para salir", severity="warning")

    def action_cancel_turn(self) -> None:
        # Con la tira de comandos visible y sin turno en curso, Esc la cierra
        # (como en opencode) en vez de intentar cancelar nada.
        if self._slash_visible and not self._busy:
            self._hide_slash_hint()
            return
        if self.on_cancel is not None:
            with contextlib.suppress(Exception):
                self.on_cancel()
        self._exit_armed = False

    def action_copy_selection(self) -> None:
        """Copia la selección del panel al portapapeles (sistema + Textual).

        Textual gestiona la selección del panel (mouse arrastrando / shift);
        action_copy_text() la pasa al clipboard de la app, y acá además la
        volcamos a pbcopy para que sea pegable en cualquier app.
        """
        with contextlib.suppress(Exception):
            self.screen.action_copy_text()
        txt = self.clipboard
        _sel_log(f"copy_selection clipboard_len={len(txt) if txt else 0} cached_len={len(self._last_selected_text)}")
        if txt:
            self._copy_to_system(txt)

    def action_input_newline(self) -> None:
        self.query_one("#input", TextArea).insert("\n")

    # ── sugerencias de slash commands (estilo opencode) ──────────────
    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        """Tira de sugerencias al tipear '/' (↑/↓ navegar, Enter ejecuta,
        Tab completa, Esc cierra)."""
        try:
            if event.control.id != "input":
                return
            self._refresh_slash_hint(event.control.text)
        except Exception:
            pass

    def _refresh_slash_hint(self, text: str) -> None:
        from display.commands import command_names, match_commands

        matches: list[tuple[str, str]] = []
        stripped = text.strip()
        if stripped.startswith("/") and "\n" not in stripped:
            token = stripped.split()[0]
            if "/" not in token[1:]:
                if token in command_names() and len(stripped.split()) > 1:
                    # Comando ya elegido + argumentos (ej. "/resume ab12"):
                    # no hay nada más que sugerir.
                    matches = []
                else:
                    matches = match_commands(token if token != "/" else "/")
        prev = (
            self._slash_matches[self._slash_index][0]
            if self._slash_matches else None
        )
        self._slash_matches = matches
        if prev and any(n == prev for n, _ in matches):
            self._slash_index = next(
                i for i, (n, _) in enumerate(matches) if n == prev
            )
        else:
            self._slash_index = 0
        self._render_slash_hint()

    def _render_slash_hint(self) -> None:
        try:
            widget = self.query_one("#slash-hint", Static)
        except Exception:
            return
        if not self._slash_matches:
            self._slash_visible = False
            widget.display = False
            return
        lines = []
        for i, (name, desc) in enumerate(self._slash_matches):
            mark = "›" if i == self._slash_index else " "
            lines.append(f"{mark} {name:<9} {desc}")
        lines.append("  [↑↓ navegar · enter ejecuta · tab completa · esc cierra]")
        widget.update("\n".join(lines))
        widget.display = True
        self._slash_visible = True

    def _hide_slash_hint(self) -> None:
        self._slash_matches = []
        self._slash_index = 0
        self._slash_visible = False
        with contextlib.suppress(Exception):
            self.query_one("#slash-hint", Static).display = False

    def action_slash_complete(self) -> None:
        """Tab: completa la coincidencia única (o el prefijo común); si no hay
        tira visible, indenta como siempre (cero cambio de conducta normal)."""
        if not self._slash_visible or not self._slash_matches:
            with contextlib.suppress(Exception):
                self.query_one("#input", TextArea).insert("  ")
            return
        import os as _os

        from display.commands import takes_args

        names = [n for n, _ in self._slash_matches]
        if len(names) == 1:
            completed = names[0]
        else:
            common = _os.path.commonprefix(names)
            token = self.query_one("#input", TextArea).text.strip().split()[0]
            completed = common if len(common) > len(token) else names[self._slash_index]
        try:
            ta = self.query_one("#input", TextArea)
            ta.load_text(completed + (" " if takes_args(completed) else ""))
            doc = ta.document
            last = doc.line_count - 1
            ta.move_cursor((last, len(doc.get_line(last))))
        except Exception:
            pass

    def _safe_submit(self, text: str) -> None:
        try:
            self.on_submit(text)
        except Exception:
            self.pane_writer.write(traceback.format_exc())
        finally:
            self._mark_idle()
            with contextlib.suppress(Exception):
                self.call_from_thread(self._refresh_toolbar)

    # ── API ────────────────────────────────────────────────────────────
    def dump_pane(self, n: int = 200) -> str:
        """Últimas N líneas del panel en texto plano (para /log)."""
        try:
            lines = [line.text for line in list(self.lines)]
        except Exception:
            return ""
        try:
            n = max(1, min(int(n), 3000))
        except Exception:
            n = 200
        return "\n".join(lines[-n:])

    def clear_pane(self) -> None:
        """Limpia el acumulado (/new): sesión nueva, panel nuevo."""
        self._md_active = False
        self._md_raw = ""
        with contextlib.suppress(Exception):
            self.query_one("#pane", SelectablePane).clear()

    def write(self, text: str) -> None:
        """Output del harness → panel (thread-safe)."""
        self.pane_writer.write(text)
