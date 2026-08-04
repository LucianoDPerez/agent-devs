"""Watcher de ESC para interrumpir el turno del agente durante el streaming.

El turno del agente corre en bloqueo (``loop.run_until_complete``) sin un
prompt activo, así que ESC no puede capturarse con prompt_toolkit ahí. Este
módulo deja el tty en ``cbreak`` en un hilo daemon: cuando detecta ESC
(``\\x1b``) invoca un callback thread-safe que cancela la task asyncio del
turno y restaura el modo del terminal antes de cortar.
"""

from __future__ import annotations

import contextlib
import os
import select
import sys
import termios
import threading
import time
import tty


class EscWatcher:
    """Detecta ESC en stdin mientras el agente streama.

    Corre en un hilo daemon con el tty en ``cbreak`` (Ctrl+C sigue
    disparando SIGINT). Ante ESC llama a ``cancel_cb`` (thread-safe, se
    espera que haga ``loop.call_soon_threadsafe(task.cancel)``).
    """

    def __init__(self, cancel_cb):
        self._cancel_cb = cancel_cb
        self._stop = threading.Event()
        self.interrupted = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self):
        if not sys.stdin.isatty() or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        fd = sys.stdin.fileno()
        old = None
        try:
            old = termios.tcgetattr(fd)
            tty.setcbreak(fd)
            while not self._stop.is_set():
                r, _, _ = select.select([fd], [], [], 0.1)
                if not r:
                    continue
                try:
                    # \x1b es ESC; si llega justo antes de una secuencia
                    # (\\x1b[A = flecha) igual cuenta como interrupción:
                    # durante el streaming el usuario no navega el buffer.
                    if os.read(fd, 1) == b"\x1b":
                        self.interrupted.set()
                        self._cancel_cb()
                        # Pequeño margen para que el hilo principal procese.
                        time.sleep(0.05)
                        break
                except OSError:
                    break
        except (termios.error, ValueError):
            pass
        finally:
            if old is not None:
                with contextlib.suppress(termios.error, ValueError, OSError):
                    termios.tcsetattr(fd, termios.TCSADRAIN, old)

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
