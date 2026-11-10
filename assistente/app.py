"""Liga as peças: fontes de áudio, detecção de fala, Whisper, Claude e a página."""
from __future__ import annotations

import collections
import logging
import os
import queue
import sys
import threading
import time
from pathlib import Path

import numpy as np

from .audio import FonteArquivo, FonteComputador, FonteMicrofone, computador_disponivel, listar_microfones
from .config import Config
from .eventos import Barramento
from .ia import MODELOS_IA, AssistenteIA
from .sessao import Sessao
from .transcricao import Transcritor, TranscritorDeTeste
from .vad import Segmentador

log = logging.getLogger(__name__)

MAX_ATRASO = 6  # trechos esperando transcrição antes de avisar
MAX_JUNTOS_S = 24.0  # o Whisper analisa janelas de 30 s
FIM = "__fim__"  # marca, na fila de áudio, que a escuta parou


class App:
    def __init__(self, config: Config | None = None, transcritor_de_teste: bool = False):
        self.config = config or Config()
        self.barramento = Barramento()
        self.sessao = Sessao(self.config.pasta_sessoes(), self.barramento)
        self.ia = AssistenteIA(self.config, self.sessao, ao_ocupado=self._ia_ocupada)
        self._classe_transcritor = TranscritorDeTeste if transcritor_de_teste else Transcritor
        self.transcritor = None
        self._trava = threading.RLock()
        self.ouvindo = False
        self.simulando = ""
        self.status = "Iniciando..."
        self.status_tipo = "info"
        self.encerrando = False
        self._fontes: list = []
        self._fila_audio: queue.Queue = queue.Queue(maxsize=3000)
        self._fila_trechos: queue.Queue = queue.Queue()
        self._geracao = 0  # muda a cada vez que começa a ouvir; áudio de escutas antigas é descartado

    # --- ciclo de vida -------------------------------------------------------------
    def iniciar(self) -> None:
        threading.Thread(target=self._laco_vad, name="vad", daemon=True).start()
        threading.Thread(target=self._laco_transcricao, name="transcricao", daemon=True).start()
        threading.Thread(target=self._laco_automatico, name="automatico", daemon=True).start()
        self._atualizar_descricao_sessao()
        if self.config["motor"] == "whisper":
            self.carregar_whisper()
        else:
            self._definir_status("Pronto. Transcrição pelo Chrome (Google).", "pronto")

    def encerrar(self) -> None:
        self.encerrando = True
        try:
            self.parar()
        except Exception:
            pass

    def carregar_whisper(self) -> None:
        cfg = self.config
        transcritor = self._classe_transcritor(
            cfg["modelo_whisper"], cfg["idioma"], cfg["vocabulario"], ao_status=self._status_whisper)
        with self._trava:
            self.transcritor = transcritor
        threading.Thread(target=transcritor.carregar, name="carregar-whisper", daemon=True).start()

    def _status_whisper(self, texto: str, pronto: bool = False, erro: bool = False) -> None:
        self._definir_status(texto, "erro" if erro else ("pronto" if pronto else "info"))

    def _definir_status(self, texto: str, tipo: str = "info") -> None:
        self.status, self.status_tipo = texto, tipo
        self.barramento.publicar("status", texto=texto, nivel=tipo)

    def _aviso(self, texto: str) -> None:
        self.barramento.publicar("aviso", texto=texto)

    def _ia_ocupada(self, ocupada: bool) -> None:
        self.barramento.publicar("ia_ocupada", ocupada=ocupada)

    def _atualizar_descricao_sessao(self) -> None:
        cfg = self.config
        nomes_modo = {"audiencia": "Audiência ou oitiva", "reuniao": "Reunião ou atendimento", "livre": "Livre"}
        modelo = MODELOS_IA.get(cfg["modelo_ia"], {}).get("nome", cfg["modelo_ia"])
        motor = "Whisper no computador" if cfg["motor"] == "whisper" else "Chrome (Google)"
        self.sessao.descricao = f"Modo: {nomes_modo.get(cfg['modo'], cfg['modo'])}. IA: {modelo}. Transcrição: {motor}."
        self.sessao.contexto = cfg["contexto"]
        self.sessao.pasta = cfg.pasta_sessoes()

    # --- estado para a página ------------------------------------------------------
    def estado(self) -> dict:
        return {
            "config": self.config.publico(),
            "ouvindo": self.ouvindo,
            "simulando": self.simulando,
            "status": {"texto": self.status, "nivel": self.status_tipo},
            "ia_ocupada": self.ia.ocupado,
            "computador_disponivel": computador_disponivel(),
            "modelos_ia": {k: v["nome"] for k, v in MODELOS_IA.items()},
            **self.sessao.estado(),
        }

    def dispositivos(self) -> dict:
        dados = listar_microfones()
        dados["computador_disponivel"] = computador_disponivel()
        return dados

    def atualizar_config(self, novos: dict) -> dict:
        mudou = self.config.atualizar(novos)
        if self.transcritor is not None:
            if "vocabulario" in mudou:
                self.transcritor.vocabulario = self.config["vocabulario"]
            if "idioma" in mudou:
                self.transcritor.idioma = self.config["idioma"]
        precisa_whisper = self.config["motor"] == "whisper"
        trocou_modelo = "modelo_whisper" in mudou
        ligou_whisper = "motor" in mudou and precisa_whisper
        if precisa_whisper and (trocou_modelo or ligou_whisper or self.transcritor is None):
            if not (ligou_whisper and not trocou_modelo and self.transcritor is not None and self.transcritor.pronto.is_set()):
                self.carregar_whisper()
        if "motor" in mudou and not precisa_whisper:
            self._definir_status("Pronto. Transcrição pelo Chrome (Google).", "pronto")
        if mudou:
            self._atualizar_descricao_sessao()
        self.barramento.publicar("config", config=self.config.publico())
        return mudou

    # --- escutar -----------------------------------------------------------------
    def iniciar_escuta(self) -> None:
        with self._trava:
            if self.ouvindo:
                return
            cfg = self.config
            self._geracao += 1
            if cfg["motor"] == "whisper":
                if self.transcritor is None:
                    self.carregar_whisper()
                elif self.transcritor.erro:
                    raise RuntimeError(self.transcritor.erro + " Em Ajustes, tente outro modelo ou o Chrome.")
                fontes = []
                try:
                    if cfg["fonte"] in ("microfone", "ambos"):
                        fontes.append(FonteMicrofone(self._receber_audio, cfg["microfone"], origem="mic"))
                    if cfg["fonte"] in ("computador", "ambos"):
                        fontes.append(FonteComputador(self._receber_audio, origem="pc", ao_erro=self._aviso))
                    for fonte in fontes:
                        fonte.iniciar()
                except Exception as erro:
                    for fonte in fontes:
                        try:
                            fonte.parar()
                        except Exception:
                            pass
                    raise RuntimeError(str(erro)) from erro
                self._fontes = fontes
            self.ouvindo = True
        self.barramento.publicar("ouvindo", ativo=True, simulando=self.simulando)

    def simular(self, caminho: Path, velocidade: float = 1.0) -> None:
        """Toca um arquivo como se fosse a conversa ao vivo (para testar)."""
        if self.config["motor"] != "whisper":
            raise RuntimeError("O teste com arquivo usa o Whisper. Em Ajustes, escolha a transcrição pelo Whisper.")
        self.parar()
        if self.transcritor is None:
            self.carregar_whisper()
        with self._trava:
            self._geracao += 1
        fonte = FonteArquivo(caminho, self._receber_audio, origem="arquivo", velocidade=velocidade,
                             ao_terminar=self._fim_simulacao)
        fonte.iniciar()  # lê o arquivo inteiro antes de começar a tocar
        with self._trava:
            self._fontes = [fonte]
            self.ouvindo = True
            self.simulando = Path(caminho).name
        self.barramento.publicar("ouvindo", ativo=True, simulando=self.simulando)
        self._aviso(f"Tocando {self.simulando} ({fonte.duracao_s / 60:.1f} min) como se fosse ao vivo.")

    def _fim_simulacao(self) -> None:
        def depois():
            time.sleep(1.5)  # deixa o último trecho chegar ao Whisper
            self.parar()
            self._aviso("O arquivo de teste terminou.")
        threading.Thread(target=depois, daemon=True).start()

    def parar(self) -> None:
        with self._trava:
            fontes, self._fontes = self._fontes, []
            estava = self.ouvindo
            self.ouvindo = False
            self.simulando = ""
        for fonte in fontes:
            try:
                fonte.parar()
            except Exception:
                log.exception("Falha ao parar a fonte")
        if fontes:  # avisa a detecção de fala para fechar o último trecho
            self._fila_audio.put((self._geracao, FIM, None, time.time()))
        if estava:
            self.barramento.publicar("ouvindo", ativo=False, simulando="")

    def _receber_audio(self, origem: str, bloco: np.ndarray) -> None:
        """Chamado pelas fontes (inclusive na thread do PortAudio): só enfileira."""
        item = (self._geracao, origem, bloco, time.time())
        try:
            self._fila_audio.put_nowait(item)
        except queue.Full:
            try:
                self._fila_audio.get_nowait()
            except queue.Empty:
                pass
            self._fila_audio.put_nowait(item)

    def adicionar_texto_navegador(self, texto: str) -> None:
        texto = " ".join((texto or "").split())
        if texto and self.ouvindo:
            self.sessao.adicionar_linha("navegador", texto)

    # --- laços em segundo plano ----------------------------------------------------
    def _laco_vad(self) -> None:
        """Só esta thread mexe nos segmentadores (um por fonte de áudio)."""
        geracao_atual = None
        segmentadores: dict[str, Segmentador] = {}
        inicios: dict[str, float] = {}
        niveis: dict[str, float] = {}
        ultimo_aviso = ultimo_nivel = 0.0
        while not self.encerrando:
            try:
                geracao, origem, bloco, momento = self._fila_audio.get(timeout=0.5)
            except queue.Empty:
                continue
            if geracao < self._geracao and origem != FIM:
                continue  # áudio de uma escuta que já acabou
            if geracao != geracao_atual:
                geracao_atual, segmentadores, inicios, niveis = geracao, {}, {}, {}
            if origem == FIM:
                for nome, seg in segmentadores.items():
                    for trecho in seg.finalizar():
                        self._fila_trechos.put((geracao, nome, trecho, inicios[nome]))
                segmentadores, inicios, niveis = {}, {}, {}
                self.barramento.publicar("nivel", niveis={}, fala={})
                continue
            seg = segmentadores.get(origem)
            if seg is None:
                try:
                    seg = Segmentador()
                except Exception as erro:
                    self._aviso(f"Falha ao iniciar a detecção de fala: {erro}")
                    continue
                segmentadores[origem] = seg
                inicios[origem] = momento - len(bloco) / 16000
            for trecho in seg.alimentar(bloco):
                self._fila_trechos.put((geracao, origem, trecho, inicios[origem]))
                atraso = self._fila_trechos.qsize()
                if atraso > MAX_ATRASO and time.monotonic() - ultimo_aviso > 30:
                    ultimo_aviso = time.monotonic()
                    self._aviso(f"A transcrição está atrasada ({atraso} trechos na fila). "
                                "Se continuar, escolha um modelo mais rápido do Whisper em Ajustes.")
            rms = float(np.sqrt(np.mean(np.square(bloco)))) if len(bloco) else 0.0
            nivel = max(0.0, min(1.0, (20 * np.log10(rms + 1e-9) + 60) / 60))
            niveis[origem] = max(nivel, niveis.get(origem, 0.0) * 0.6)
            agora = time.monotonic()
            if agora - ultimo_nivel > 0.2:
                ultimo_nivel = agora
                self.barramento.publicar("nivel", niveis=dict(niveis),
                                         fala={o: s.em_fala for o, s in segmentadores.items()})

    def _laco_transcricao(self) -> None:
        """Transcreve os trechos. Se vários estiverem esperando (computador lento), junta os da
        mesma fonte numa chamada só: o Whisper gasta quase o mesmo tempo para 3 s ou para 25 s."""
        pendentes: collections.deque = collections.deque()
        pausa = np.zeros(int(0.3 * 16000), dtype=np.float32)
        while not self.encerrando:
            if not pendentes:
                try:
                    pendentes.append(self._fila_trechos.get(timeout=0.5))
                except queue.Empty:
                    continue
            while True:
                try:
                    pendentes.append(self._fila_trechos.get_nowait())
                except queue.Empty:
                    break
            geracao, origem, trecho, inicio = pendentes.popleft()
            audios, duracao = [trecho.audio], trecho.duracao_s
            while (pendentes and pendentes[0][0] == geracao and pendentes[0][1] == origem
                   and duracao + pendentes[0][2].duracao_s <= MAX_JUNTOS_S):
                proximo = pendentes.popleft()[2]
                audios += [pausa, proximo.audio]
                duracao += 0.3 + proximo.duracao_s
            transcritor = self.transcritor
            if transcritor is None:
                continue
            try:
                texto = transcritor.transcrever(np.concatenate(audios))
            except Exception as erro:
                log.exception("Falha ao transcrever")
                self._aviso(f"Falha ao transcrever um trecho: {erro}")
                continue
            if texto:
                self.sessao.adicionar_linha(origem, texto, inicio + trecho.inicio_s)

    def _laco_automatico(self) -> None:
        while not self.encerrando:
            time.sleep(1.0)
            cfg = self.config
            if not (self.ouvindo and cfg["auto_sugerir"] and cfg.chave_api()):
                continue
            if self.sessao.caracteres_novos() < 60:
                continue
            if time.time() - self.ia.ultimo_pedido < cfg["intervalo_s"]:
                continue
            self.ia.pedir("auto")

    # --- pedidos da página ---------------------------------------------------------
    def sugerir_agora(self) -> None:
        self.ia.pedir("agora")

    def perguntar(self, pergunta: str) -> None:
        pergunta = (pergunta or "").strip()
        if not pergunta:
            raise ValueError("Escreva a pergunta.")
        self.ia.pedir("pergunta", pergunta[:2000])

    def nova_sessao(self) -> None:
        self._atualizar_descricao_sessao()
        self.sessao.nova()
        self._atualizar_descricao_sessao()

    def abrir_pasta(self) -> str:
        pasta = self.config.pasta_sessoes()
        pasta.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(pasta))  # type: ignore[attr-defined]
        return str(pasta)
