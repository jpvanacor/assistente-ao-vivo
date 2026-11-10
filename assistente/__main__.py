"""Ponto de entrada: python -m assistente"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
import webbrowser
from pathlib import Path

from . import __version__
from .app import App
from .config import Config
from .servidor import abrir_servidor

PORTA = 8765


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="assistente", description="Assistente ao Vivo")
    parser.add_argument("--porta", type=int, default=PORTA)
    parser.add_argument("--dados", type=Path, help="pasta de dados (config.json e sessões)")
    parser.add_argument("--sem-navegador", action="store_true", help="não abre o navegador")
    parser.add_argument("--transcritor-de-teste", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--detalhes", action="store_true", help="mostra mensagens de diagnóstico")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.detalhes else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for barulhento in ("httpx", "httpx2", "httpcore", "anthropic", "faster_whisper", "urllib3"):
        logging.getLogger(barulhento).setLevel(logging.WARNING)

    config = Config(args.dados) if args.dados else Config()
    app = App(config, transcritor_de_teste=args.transcritor_de_teste)
    try:
        servidor, porta = abrir_servidor(app, args.porta)
    except OSError as erro:
        print(f"Não consegui abrir o servidor da página: {erro}")
        return 1
    url = f"http://127.0.0.1:{porta}/"
    if servidor is None:
        print(f"O Assistente ao Vivo já está aberto. Abrindo {url}")
        if not args.sem_navegador:
            webbrowser.open(url)
        return 0

    app.iniciar()
    print()
    print(f"  Assistente ao Vivo {__version__}")
    print(f"  Página: {url}")
    print(f"  Dados e sessões: {config.pasta}")
    print("  Para encerrar, feche esta janela ou use 'Fechar o programa' na página.")
    print()
    if not args.sem_navegador:
        threading.Timer(0.8, webbrowser.open, args=(url,)).start()
    try:
        servidor.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        app.encerrar()
        servidor.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
