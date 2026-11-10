"""O programa inteiro rodando: página, eventos, arquivo tocado ao vivo, detecção de fala,
transcritor de teste e o SDK do Claude falando com um servidor falso da API."""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from assistente.app import App
from assistente.config import Config
from assistente.servidor import Servidor
from assistente.transcricao import TranscritorDeTeste
from tests.mock_anthropic import MockAnthropic

DADOS = Path(__file__).parent / "_dados"
CHAVE = "sk-ant-teste-0000-1234"


class Ouvinte:
    """Lê o fluxo de eventos (SSE) da página."""

    def __init__(self, base: str):
        self.eventos: list[dict] = []
        self._resposta = urllib.request.urlopen(base + "/eventos", timeout=60)
        threading.Thread(target=self._ler, daemon=True).start()

    def _ler(self):
        try:
            for bruto in self._resposta:
                linha = bruto.decode("utf-8").strip()
                if linha.startswith("data: "):
                    self.eventos.append(json.loads(linha[6:]))
        except Exception:
            pass

    def esperar(self, condicao, limite=30.0):
        fim = time.time() + limite
        while time.time() < fim:
            achados = [e for e in list(self.eventos) if condicao(e)]
            if achados:
                return achados
            time.sleep(0.05)
        raise AssertionError("evento não chegou; últimos: " + json.dumps(self.eventos[-5:], ensure_ascii=False)[:1500])


def post(base, caminho, dados=None, cabecalho=True, corpo=None, host=None):
    cabecalhos = {"Content-Type": "application/json"}
    if cabecalho:
        cabecalhos["X-Assistente"] = "1"
    if host:
        cabecalhos["Host"] = host
    pedido = urllib.request.Request(base + caminho, data=corpo if corpo is not None else json.dumps(dados or {}).encode(),
                                    headers=cabecalhos, method="POST")
    try:
        with urllib.request.urlopen(pedido, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


@pytest.fixture()
def ambiente(tmp_path, monkeypatch):
    if not (DADOS / "jfk.flac").exists():
        pytest.skip("falta tests/_dados/jfk.flac")
    api = MockAnthropic().iniciar()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", api.url)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    TranscritorDeTeste.atraso_s = 0.05
    TranscritorDeTeste.chamadas = []
    config = Config(tmp_path / "dados")
    config.atualizar({"api_key": CHAVE, "intervalo_s": 5, "auto_sugerir": True, "contexto": "Caso de teste: contrato da prefeitura."})
    app = App(config, transcritor_de_teste=True)
    servidor = Servidor(app, 0)
    threading.Thread(target=servidor.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
    app.iniciar()
    base = f"http://127.0.0.1:{servidor.porta}"
    yield app, api, base, config
    app.encerrar()
    servidor.shutdown()
    servidor.server_close()
    api.shutdown()


def test_fluxo_completo(ambiente):
    app, api, base, config = ambiente
    ouvinte = Ouvinte(base)
    ouvinte.esperar(lambda e: e["tipo"] == "estado")

    # toca a gravação como se fosse ao vivo (4x mais rápido)
    corpo = (DADOS / "jfk.flac").read_bytes()
    codigo, resposta = post(base, "/api/simular?nome=jfk.flac&velocidade=4", corpo=corpo)
    assert codigo == 200, resposta
    ouvinte.esperar(lambda e: e["tipo"] == "ouvindo" and e["ativo"])
    ouvinte.esperar(lambda e: e["tipo"] == "linha", 20)
    ouvinte.esperar(lambda e: e["tipo"] == "nivel" and e["niveis"].get("arquivo", 0) > 0.3, 10)

    # a primeira sugestão automática vem vazia (SEM_SUGESTAO) e não aparece como texto
    vazia = ouvinte.esperar(lambda e: e["tipo"] == "sugestao_fim" and e["estado"] == "vazia", 20)[0]
    assert vazia["texto"] == ""
    trechos_da_vazia = [e for e in ouvinte.eventos if e["tipo"] == "sugestao_trecho" and e["id"] == vazia["id"]]
    assert trechos_da_vazia == [], "SEM_SUGESTAO não pode ir para a tela"

    ouvinte.esperar(lambda e: e["tipo"] == "ouvindo" and not e["ativo"], 20)  # o arquivo terminou

    # sugestão pedida na hora, em fluxo
    assert post(base, "/api/sugerir")[0] == 200
    fim = ouvinte.esperar(lambda e: e["tipo"] == "sugestao_fim" and e["estado"] == "pronta", 20)[0]
    assert "Pergunte:" in fim["texto"] and fim["latencia_s"] is not None
    pedacos = [e for e in ouvinte.eventos if e["tipo"] == "sugestao_trecho" and e["id"] == fim["id"]]
    assert len(pedacos) > 5, "o texto deve chegar aos poucos"
    assert fim["custo"] > 0 and fim["custo_total"] >= fim["custo"]

    # pergunta direta
    assert post(base, "/api/perguntar", {"texto": "Qual foi o prazo combinado?"})[0] == 200
    ouvinte.esperar(lambda e: e["tipo"] == "sugestao_fim" and e["estado"] == "pronta" and e["id"] != fim["id"], 20)

    # o que foi enviado à API
    pedidos = api.pedidos
    assert len(pedidos) >= 3
    p = pedidos[-1]
    assert p["model"] == "claude-haiku-5-5"
    assert p["thinking"] == {"type": "disabled"}
    assert p["output_config"] == {"effort": "low"}
    assert p["_headers"].get("Authorization") == f"Bearer {CHAVE}"
    assert p["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "Caso de teste" in p["system"][-1]["text"]
    textos = [b["text"] for b in p["messages"][0]["content"]]
    assert any("Trecho 1 com" in t for t in textos)
    assert "Qual foi o prazo combinado?" in textos[-1]

    # sessão gravada em Markdown
    estado = app.estado()
    arquivo = Path(estado["arquivo"])
    md = arquivo.read_text(encoding="utf-8")
    assert "## Contexto" in md and "Trecho 1 com" in md and "Pergunta: Qual foi o prazo combinado?" in md
    assert "SEM_SUGESTAO" not in md
    assert " - sessao.md" in arquivo.name and "–" not in arquivo.name

    # Sonnet usa o modo sem raciocínio prévio aceito por ele
    post(base, "/api/config", {"modelo_ia": "claude-sonnet-5-5"})
    post(base, "/api/sugerir")
    ouvinte.esperar(lambda e: e["tipo"] == "sugestao_fim" and len(api.pedidos) >= 4, 20)
    assert api.pedidos[-1]["model"] == "claude-sonnet-5-5"
    assert api.pedidos[-1]["thinking"] == {"type": "between_tools"}


def test_erros_e_seguranca(ambiente):
    app, api, base, config = ambiente
    ouvinte = Ouvinte(base)
    # chave recusada
    api.status_forcado = 401
    post(base, "/api/sugerir")
    erro = ouvinte.esperar(lambda e: e["tipo"] == "sugestao_erro", 20)[0]
    assert "chave da API foi recusada" in erro["erro"]
    # sem chave
    config.atualizar({"api_key": ""})
    post(base, "/api/sugerir")
    ouvinte.esperar(lambda e: e["tipo"] == "sugestao_erro" and "Falta a chave" in e["erro"], 10)
    # pedidos de fora são recusados
    assert post(base, "/api/parar", cabecalho=False)[0] == 403
    assert post(base, "/api/parar", host="evil.example:80")[0] == 403
    req = urllib.request.Request(base + "/api/estado", headers={"Host": "evil.example"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=10)
    assert e.value.code == 403
    # a chave nunca vai para a página
    config.atualizar({"api_key": CHAVE})
    with urllib.request.urlopen(base + "/api/estado", timeout=10) as r:
        bruto = r.read().decode()
    assert CHAVE not in bruto and '"tem_chave": true' in bruto
    # ajuste inválido vira mensagem clara
    codigo, resposta = post(base, "/api/config", {"modo": "festa"})
    assert codigo == 400 and "Ajuste inválido" in resposta["erro"]
    # arquivo que não é áudio
    codigo, resposta = post(base, "/api/simular?nome=x.mp3", corpo=b"isto nao e audio" * 100)
    assert codigo == 400 and "Não consegui ler o arquivo" in resposta["erro"]


def test_computador_lento_junta_trechos(ambiente):
    """Com transcrição lenta, os trechos que esperam são juntados e a fila não cresce."""
    app, api, base, config = ambiente
    if not (DADOS / "physicsworks.wav").exists():
        pytest.skip("falta physicsworks.wav")
    config.atualizar({"auto_sugerir": False})
    TranscritorDeTeste.atraso_s = 1.5  # cada chamada demora 1,5 s
    ouvinte = Ouvinte(base)
    corpo = (DADOS / "physicsworks.wav").read_bytes()
    assert post(base, "/api/simular?nome=aula.wav&velocidade=4", corpo=corpo)[0] == 200
    ouvinte.esperar(lambda e: e["tipo"] == "ouvindo" and not e["ativo"], 90)
    time.sleep(4)
    chamadas = TranscritorDeTeste.chamadas
    linhas = [e for e in ouvinte.eventos if e["tipo"] == "linha"]
    assert len(linhas) == len(chamadas)
    # a aula dá 52 trechos; com 1,5 s por chamada, juntar é a única forma de acompanhar
    assert len(chamadas) < 40, f"{len(chamadas)} chamadas: não juntou os trechos"
    assert max(chamadas) <= 24 + 3
    assert app._fila_trechos.qsize() == 0


def test_segunda_copia_e_porta_ocupada(ambiente, tmp_path):
    """Abrir de novo não cria outra cópia; porta usada por outro programa passa para a seguinte."""
    import socket
    from assistente.servidor import abrir_servidor
    app, api, base, config = ambiente
    porta_aberta = int(base.rsplit(":", 1)[1])
    servidor, porta = abrir_servidor(app, porta_aberta)
    assert servidor is None and porta == porta_aberta
    estranho = socket.socket()
    estranho.bind(("127.0.0.1", 0))
    estranho.listen(1)
    ocupada = estranho.getsockname()[1]
    try:
        outro = App(Config(tmp_path / "outro"), transcritor_de_teste=True)
        servidor, porta = abrir_servidor(outro, ocupada)
        assert servidor is not None and porta != ocupada
        servidor.server_close()
    finally:
        estranho.close()
