"""Servidor local da página (só no próprio computador: 127.0.0.1)."""
from __future__ import annotations

import json
import logging
import queue
import re
import sys
import tempfile
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

log = logging.getLogger(__name__)

PAGINA = Path(__file__).with_name("web") / "index.html"
LIMITE_JSON = 2 * 1024 * 1024
LIMITE_ARQUIVO = 1024 * 1024 * 1024  # 1 GB


class Servidor(ThreadingHTTPServer):
    daemon_threads = True
    # No Windows, SO_REUSEADDR deixaria duas cópias abrirem a mesma porta; no Linux, só evita
    # o bloqueio de alguns segundos depois de fechar o programa.
    allow_reuse_address = sys.platform != "win32"

    def __init__(self, app, porta: int):
        self.app = app
        super().__init__(("127.0.0.1", porta), Manipulador)
        self.porta = self.server_address[1]
        self.hosts_aceitos = {f"127.0.0.1:{self.porta}", f"localhost:{self.porta}"}


def ja_aberto(porta: int) -> bool:
    """Outra cópia do Assistente ao Vivo está respondendo nesta porta?"""
    try:
        pedido = urllib.request.Request(f"http://127.0.0.1:{porta}/api/estado")
        abridor = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with abridor.open(pedido, timeout=1.5) as resposta:
            return resposta.headers.get("Server", "").startswith("AssistenteAoVivo")
    except Exception:
        return False


def abrir_servidor(app, porta: int) -> tuple[Servidor | None, int]:
    """Abre na porta pedida ou na próxima livre. (None, porta) se já há uma cópia aberta."""
    for tentativa in range(porta, porta + 10):
        try:
            return Servidor(app, tentativa), tentativa
        except OSError:
            if ja_aberto(tentativa):
                return None, tentativa
    raise OSError(f"Nenhuma porta livre entre {porta} e {porta + 9}.")


class Manipulador(BaseHTTPRequestHandler):
    server: Servidor
    protocol_version = "HTTP/1.1"
    server_version = "AssistenteAoVivo"

    def log_message(self, formato, *args):  # silencioso
        log.debug("%s %s", self.address_string(), formato % args)

    # --- respostas -------------------------------------------------------------
    def _responder(self, codigo: int, corpo: bytes, tipo: str) -> None:
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if codigo >= 400:  # o corpo do pedido pode não ter sido lido: fecha a conexão
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(corpo)

    def _json(self, codigo: int, dados: dict) -> None:
        self._responder(codigo, json.dumps(dados, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _host_ok(self) -> bool:
        # bloqueia sites de fora que tentem falar com o programa (DNS rebinding)
        return self.headers.get("Host", "") in self.server.hosts_aceitos

    # --- GET -------------------------------------------------------------------
    def do_GET(self):
        if not self._host_ok():
            return self._json(403, {"erro": "Acesso só pelo próprio computador."})
        caminho = urlparse(self.path).path
        app = self.server.app
        if caminho in ("/", "/index.html"):
            try:
                corpo = PAGINA.read_bytes()
            except OSError:
                return self._json(500, {"erro": "Página não encontrada."})
            return self._responder(200, corpo, "text/html; charset=utf-8")
        if caminho == "/eventos":
            return self._eventos()
        if caminho == "/api/estado":
            return self._json(200, app.estado())
        if caminho == "/api/dispositivos":
            return self._json(200, app.dispositivos())
        return self._json(404, {"erro": "Não encontrado."})

    def _eventos(self) -> None:
        app = self.server.app
        fila = app.barramento.assinar()
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            inicial = json.dumps({"tipo": "estado", **app.estado()}, ensure_ascii=False)
            self.wfile.write(f"data: {inicial}\n\n".encode("utf-8"))
            self.wfile.flush()
            while not app.encerrando:
                try:
                    mensagem = fila.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(f"data: {mensagem}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            app.barramento.cancelar(fila)

    # --- POST ------------------------------------------------------------------
    def do_POST(self):
        if not self._host_ok() or self.headers.get("X-Assistente") != "1":
            return self._json(403, {"erro": "Pedido recusado."})
        url = urlparse(self.path)
        try:
            tamanho = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._json(400, {"erro": "Tamanho inválido."})
        if url.path == "/api/simular":
            return self._simular(url, tamanho)
        if tamanho > LIMITE_JSON:
            return self._json(413, {"erro": "Pedido grande demais."})
        try:
            dados = json.loads(self.rfile.read(tamanho) or b"{}") if tamanho else {}
            if not isinstance(dados, dict):
                raise ValueError
        except ValueError:
            return self._json(400, {"erro": "JSON inválido."})
        try:
            resposta = self._rota(url.path, dados)
        except KeyError:
            return self._json(404, {"erro": "Não encontrado."})
        except (ValueError, RuntimeError) as erro:
            return self._json(400, {"erro": str(erro)})
        except Exception as erro:
            log.exception("Erro em %s", url.path)
            return self._json(500, {"erro": f"Erro inesperado: {erro}"})
        return self._json(200, resposta or {"ok": True})

    def _rota(self, caminho: str, dados: dict):
        app = self.server.app
        if caminho == "/api/config":
            mudou = app.atualizar_config(dados)
            return {"ok": True, "mudou": sorted(mudou)}
        if caminho == "/api/iniciar":
            app.iniciar_escuta()
            return None
        if caminho == "/api/parar":
            app.parar()
            return None
        if caminho == "/api/sugerir":
            app.sugerir_agora()
            return None
        if caminho == "/api/perguntar":
            app.perguntar(str(dados.get("texto", "")))
            return None
        if caminho == "/api/texto":
            app.adicionar_texto_navegador(str(dados.get("texto", "")))
            return None
        if caminho == "/api/nova_sessao":
            app.nova_sessao()
            return None
        if caminho == "/api/abrir_pasta":
            return {"ok": True, "pasta": app.abrir_pasta()}
        if caminho == "/api/sair":
            threading.Thread(target=self._desligar, daemon=True).start()
            return None
        raise KeyError(caminho)

    def _desligar(self) -> None:
        app = self.server.app
        app.encerrar()
        self.server.shutdown()

    def _simular(self, url, tamanho: int) -> None:
        """Recebe um arquivo de áudio da página e toca como se fosse ao vivo."""
        if tamanho <= 0:
            return self._json(400, {"erro": "Arquivo vazio."})
        if tamanho > LIMITE_ARQUIVO:
            return self._json(413, {"erro": "Arquivo grande demais (limite de 1 GB)."})
        parametros = parse_qs(url.query)
        nome = (parametros.get("nome") or ["audio"])[0]
        nome = re.sub(r"[^\w.\- ]+", "_", nome)[:120] or "audio"
        try:
            velocidade = float((parametros.get("velocidade") or ["1"])[0])
        except ValueError:
            velocidade = 1.0
        pasta = Path(tempfile.gettempdir()) / "assistente-ao-vivo"
        pasta.mkdir(parents=True, exist_ok=True)
        for antigo in pasta.iterdir():  # só guarda o arquivo de teste mais recente
            try:
                antigo.unlink()
            except OSError:
                pass
        destino = pasta / nome
        restante = tamanho
        with destino.open("wb") as arquivo:
            while restante > 0:
                pedaco = self.rfile.read(min(1024 * 1024, restante))
                if not pedaco:
                    break
                arquivo.write(pedaco)
                restante -= len(pedaco)
        if restante:
            return self._json(400, {"erro": "O envio do arquivo foi interrompido."})
        try:
            self.server.app.simular(destino, velocidade)
        except (RuntimeError, ValueError) as erro:
            return self._json(400, {"erro": str(erro)})
        return self._json(200, {"ok": True})
