"""Servidor falso da API do Claude, para testar o fluxo (streaming) com o SDK de verdade."""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class MockAnthropic(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Manipulador)
        self.pedidos: list[dict] = []
        self.respostas_auto = ["SEM_SUGESTAO"]
        self.resposta_padrao = (
            "- Pergunte: quando o contrato foi assinado e por quem?\n"
            "- [Não verificado] Base legal: art. 10 da Lei 8.429/1992.\n"
            "- Anote: o prazo de 15 dias combinado."
        )
        self.resposta_pergunta = ""
        self.status_forcado: int | None = None
        self.atraso_s = 0.02

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def iniciar(self) -> "MockAnthropic":
        threading.Thread(target=self.serve_forever, daemon=True).start()
        return self


class _Manipulador(BaseHTTPRequestHandler):
    server: MockAnthropic
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        corpo = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        corpo["_headers"] = dict(self.headers)
        self.server.pedidos.append(corpo)
        if self.server.status_forcado:
            erro = json.dumps({"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}).encode()
            self.send_response(self.server.status_forcado)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(erro)))
            self.end_headers()
            self.wfile.write(erro)
            return
        instrucao = corpo["messages"][-1]["content"][-1]["text"]
        if "responda apenas SEM_SUGESTAO" in instrucao and self.server.respostas_auto:
            texto = self.server.respostas_auto.pop(0)
        elif instrucao.startswith("Pergunta do assessor") and self.server.resposta_pergunta:
            texto = self.server.resposta_pergunta
        else:
            texto = self.server.resposta_padrao
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def evento(tipo, dados):
            self.wfile.write(f"event: {tipo}\ndata: {json.dumps(dados)}\n\n".encode())
            self.wfile.flush()

        evento("message_start", {"type": "message_start", "message": {
            "id": "msg_teste", "type": "message", "role": "assistant", "model": corpo["model"],
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 1500, "output_tokens": 1,
                      "cache_creation_input_tokens": 400, "cache_read_input_tokens": 2000}}})
        evento("content_block_start", {"type": "content_block_start", "index": 0,
                                       "content_block": {"type": "text", "text": ""}})
        for i in range(0, len(texto), 7):
            time.sleep(self.server.atraso_s)
            evento("content_block_delta", {"type": "content_block_delta", "index": 0,
                                           "delta": {"type": "text_delta", "text": texto[i:i + 7]}})
        evento("content_block_stop", {"type": "content_block_stop", "index": 0})
        evento("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                 "usage": {"output_tokens": 60}})
        evento("message_stop", {"type": "message_stop"})
        self.close_connection = True
