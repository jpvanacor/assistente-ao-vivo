"""Testes das peças isoladas. Rodar na raiz do projeto: python -m pytest tests -q"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from assistente.audio import Reamostrador, ler_audio
from assistente.config import Config
from assistente.ia import CACHE, LINHAS_POR_BLOCO, Pedido, montar_pedido
from assistente.sessao import Linha
from assistente.transcricao import limpar_texto
from assistente.vad import JANELA, Segmentador, VadSilero

DADOS = Path(__file__).parent / "_dados"


def _audio(nome: str) -> np.ndarray:
    caminho = DADOS / nome
    if not caminho.exists():
        pytest.skip(f"falta o arquivo de teste {nome}")
    return ler_audio(caminho)


def test_ler_audio_flac_e_wav():
    jfk = _audio("jfk.flac")
    assert jfk.dtype == np.float32 and abs(len(jfk) / 16000 - 11.0) < 0.1
    assert 0.1 < np.abs(jfk).max() <= 1.0
    fisica = _audio("physicsworks.wav")
    assert len(fisica) / 16000 > 60


# --- reamostragem --------------------------------------------------------------
@pytest.mark.parametrize("taxa", [48000, 44100, 32000, 22050])
def test_reamostrador_independe_do_tamanho_dos_blocos(taxa):
    t = np.arange(int(taxa * 2.0)) / taxa
    sinal = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    inteiro = Reamostrador(taxa)(sinal)
    r = Reamostrador(taxa)
    rng = np.random.default_rng(1)
    partes, i = [], 0
    while i < len(sinal):
        n = int(rng.integers(1, 3000))
        partes.append(r(sinal[i:i + n]))
        i += n
    em_blocos = np.concatenate(partes)
    assert abs(len(inteiro) - 32000) <= 2
    assert len(em_blocos) == len(inteiro)
    assert np.allclose(em_blocos, inteiro, atol=1e-5)
    espectro = np.abs(np.fft.rfft(inteiro * np.hanning(len(inteiro))))
    pico = np.argmax(espectro) * 16000 / len(inteiro)
    assert abs(pico - 440) < 3


def test_reamostrador_sem_conversao():
    x = np.random.default_rng(0).standard_normal(1000).astype(np.float32)
    assert np.array_equal(Reamostrador(16000)(x), x)


# --- detecção de fala ----------------------------------------------------------
def test_vad_em_fluxo_igual_ao_audio_inteiro():
    from faster_whisper.vad import get_vad_model
    audio = _audio("jfk.flac")
    audio = audio[: len(audio) // JANELA * JANELA]
    referencia = get_vad_model()(audio.copy())
    vad = VadSilero()
    janelas = audio.reshape(-1, JANELA)
    partes, i = [], 0
    for n in [1, 3, 7, 2, 50, 13] * 200:
        if i >= len(janelas):
            break
        partes.append(vad.probabilidades(janelas[i:i + n]))
        i += n
    em_fluxo = np.concatenate(partes)
    # a última janela difere de propósito no faster-whisper (ele zera o fim dela)
    assert np.allclose(em_fluxo[:-1], referencia[:-1], atol=1e-4)


def _segmentar(audio: np.ndarray, **kw):
    seg = Segmentador(**kw)
    trechos = []
    for i in range(0, len(audio), 1600):
        trechos += seg.alimentar(audio[i:i + 1600])
    trechos += seg.alimentar(np.zeros(16000, dtype=np.float32))
    trechos += seg.finalizar()
    return trechos


def test_segmentador_em_fala_real():
    audio = _audio("physicsworks.wav")
    trechos = _segmentar(audio)
    duracao = len(audio) / 16000
    assert len(trechos) >= duracao / 12, "fala corrida deve ser cortada em trechos"
    for t in trechos:
        assert 0.3 <= t.duracao_s <= 10.0 + 0.05
    inicios = [t.inicio_s for t in trechos]
    assert inicios == sorted(inicios)
    for a, b in zip(trechos, trechos[1:]):
        assert a.inicio_s + a.duracao_s <= b.inicio_s + 0.001, "trechos não podem se sobrepor"
    # cobre quase toda a fala que o faster-whisper detecta no áudio inteiro
    from faster_whisper.vad import VadOptions, get_speech_timestamps
    falas = get_speech_timestamps(audio, VadOptions(min_silence_duration_ms=600))
    total_fala = sum(f["end"] - f["start"] for f in falas) / 16000
    coberto = sum(t.duracao_s for t in trechos)
    assert coberto >= 0.9 * total_fala


def test_segmentador_trecho_tem_o_audio_certo():
    audio = _audio("jfk.flac")
    trechos = _segmentar(audio)
    assert trechos
    for t in trechos:
        ini = int(round(t.inicio_s * 16000))
        original = audio[ini: ini + len(t.audio)]
        assert np.array_equal(original, t.audio[: len(original)])


def test_segmentador_silencio_e_ruido_nao_geram_trechos():
    rng = np.random.default_rng(3)
    ruido = (rng.standard_normal(16000 * 20) * 0.01).astype(np.float32)
    assert _segmentar(np.zeros(16000 * 10, dtype=np.float32)) == []
    assert _segmentar(ruido) == []


# --- limpeza do texto ----------------------------------------------------------
@pytest.mark.parametrize("texto", [
    "Legendas pela comunidade Amara.org", "Obrigado por assistir!", "Inscreva-se no canal.", "...", "  ", "é",
])
def test_limpar_texto_descarta_alucinacoes(texto):
    assert limpar_texto(texto) == ""


def test_limpar_texto_mantem_fala_normal_e_corta_eco_do_vocabulario():
    assert limpar_texto("  O contrato foi assinado   em março. ") == "O contrato foi assinado em março."
    assert limpar_texto("Sim.") == "Sim." and limpar_texto("Não.") == "Não."
    vocab = "Ministério Público, Promotoria de Justiça, São Leopoldo"
    assert limpar_texto("Ministério Público, Promotoria de Justiça.", vocab) == ""
    assert limpar_texto("O Ministério Público pediu os documentos.", vocab) != ""


# --- pedido ao Claude ----------------------------------------------------------
def _linhas(n: int, tamanho: int = 40) -> list[Linha]:
    return [Linha(i, f"10:00:{i % 60:02d}", "mic", f"fala {i} " + "x" * tamanho) for i in range(n)]


def _marcados(blocos):
    return [i for i, b in enumerate(blocos) if b.get("cache_control") == CACHE]


def test_pedido_estrutura_e_cache():
    system, mensagens = montar_pedido(_linhas(25), "Resumo do caso.", "audiencia", Pedido("auto"), ["- Pergunte: x"], False)
    assert [b.get("cache_control") for b in system] == [None, CACHE]
    assert "audiência ou oitiva" in system[0]["text"]
    assert mensagens[0]["role"] == "user"
    conteudo = mensagens[0]["content"]
    # cabeçalho + 3 blocos (10, 10, 5) + instrução
    assert len(conteudo) == 1 + 3 + 1
    assert _marcados(conteudo) == [2], "cache no último bloco completo"
    assert "SEM_SUGESTAO" in conteudo[-1]["text"] and "- Pergunte: x" in conteudo[-1]["text"]
    assert all(b["text"] for b in conteudo), "bloco vazio não pode ter cache"


def test_pedido_prefixo_estavel_quando_a_conversa_cresce():
    _, a = montar_pedido(_linhas(23), "", "reuniao", Pedido("agora"), [], False)
    _, b = montar_pedido(_linhas(37), "", "reuniao", Pedido("agora"), [], False)
    blocos_a, blocos_b = a[0]["content"], b[0]["content"]
    # os blocos completos do pedido anterior continuam idênticos no seguinte
    assert [x["text"] for x in blocos_a[1:3]] == [x["text"] for x in blocos_b[1:3]]


def test_pedido_sem_contexto_e_sem_falas():
    system, mensagens = montar_pedido([], "  ", "livre", Pedido("pergunta", "Qual o prazo?"), [], False)
    assert len(system) == 1 and system[0]["cache_control"] == CACHE
    conteudo = mensagens[0]["content"]
    assert "Ainda não há falas" in conteudo[1]["text"]
    assert "Qual o prazo?" in conteudo[-1]["text"]
    assert _marcados(conteudo) == []


def test_pedido_longo_corta_o_inicio_em_blocos_inteiros():
    linhas = _linhas(400, tamanho=300)  # ~120 mil caracteres
    _, mensagens = montar_pedido(linhas, "", "livre", Pedido("agora"), [], False)
    conteudo = mensagens[0]["content"]
    texto = "".join(b["text"] for b in conteudo[1:-1])
    assert len(texto) <= 62_000
    primeira = conteudo[1]["text"].split("\n")[0]
    numero = int(primeira.split("fala ")[1].split(" ")[0])
    assert numero % LINHAS_POR_BLOCO == 0
    assert "omitido" in conteudo[0]["text"]


def test_pedido_duas_fontes_rotula_as_linhas():
    linhas = [Linha(1, "10:00:01", "mic", "bom dia"), Linha(2, "10:00:03", "pc", "bom dia, doutor")]
    system, mensagens = montar_pedido(linhas, "", "reuniao", Pedido("agora"), [], True)
    assert "(Computador)" in system[0]["text"]
    assert mensagens[0]["content"][1]["text"] == "[10:00:01] (Microfone) bom dia\n[10:00:03] (Computador) bom dia, doutor"


# --- configuração --------------------------------------------------------------
def test_config_valida_grava_e_esconde_a_chave(tmp_path):
    cfg = Config(tmp_path)
    mudou = cfg.atualizar({"api_key": " sk-ant-teste-123456 ", "intervalo_s": "2", "modo": "audiencia", "inexistente": 1})
    assert set(mudou) == {"api_key", "intervalo_s", "modo"}
    assert cfg["intervalo_s"] == 5 and cfg.chave_api() == "sk-ant-teste-123456"
    with pytest.raises(ValueError):
        cfg.atualizar({"modo": "invalido"})
    publico = cfg.publico()
    assert "api_key" not in publico and publico["tem_chave"] and publico["chave_final"] == "3456"
    salvo = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert salvo["modo"] == "audiencia"
    assert Config(tmp_path)["modo"] == "audiencia"
