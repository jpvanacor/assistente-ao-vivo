"""Barramento de eventos: o que acontece no programa vai para todas as páginas abertas."""
from __future__ import annotations

import json
import queue
import threading


class Barramento:
    def __init__(self):
        self._trava = threading.Lock()
        self._filas: set[queue.Queue] = set()

    def assinar(self) -> queue.Queue:
        fila: queue.Queue = queue.Queue(maxsize=2000)
        with self._trava:
            self._filas.add(fila)
        return fila

    def cancelar(self, fila: queue.Queue) -> None:
        with self._trava:
            self._filas.discard(fila)

    def publicar(self, tipo: str, **dados) -> None:
        mensagem = json.dumps({"tipo": tipo, **dados}, ensure_ascii=False)
        with self._trava:
            filas = list(self._filas)
        for fila in filas:
            try:
                fila.put_nowait(mensagem)
            except queue.Full:  # página travada: descarta em vez de travar o programa
                pass
