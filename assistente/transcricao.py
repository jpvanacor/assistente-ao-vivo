"""Transcrição local com faster-whisper (o áudio não sai do computador)."""
from __future__ import annotations

import logging
import os
import re
import sys
import threading
import time
from typing import Callable

import numpy as np

TAXA = 16000
log = logging.getLogger(__name__)

TAMANHOS = {"base": "cerca de 150 MB", "small": "cerca de 480 MB", "medium": "cerca de 1,5 GB",
            "large-v3-turbo": "cerca de 1,6 GB"}

# Frases que o Whisper costuma "inventar" em silêncio ou ruído (vêm de legendas de vídeos).
_ALUCINACOES = re.compile(
    r"amara\.org|legendas? (pela|por|feitas?)|legendado por|legenda:|"
    r"obrigad[oa] por (assistir|ver)|inscreva-se|se inscreva|deixe (o )?seu like|"
    r"ative o sininho|até o próximo vídeo|tradução e legendas",
    re.IGNORECASE,
)


def limpar_texto(texto: str, vocabulario: str = "") -> str:
    """Normaliza espaços e descarta alucinações típicas e ecos do vocabulário."""
    t = re.sub(r"\s+", " ", texto or "").strip()
    if len(re.sub(r"[\W_]+", "", t)) < 2:
        return ""
    if _ALUCINACOES.search(t):
        return ""
    if vocabulario and len(t) > 12 and t.lower().strip(" .,;") in vocabulario.lower():
        return ""
    return t


StatusFn = Callable[..., None]


def placa_de_video_utilizavel(ctranslate2) -> bool:
    """Só usa a placa NVIDIA se ela aceitar float16 e as bibliotecas CUDA estiverem instaladas.
    Placas fracas (ex.: MX250) ou sem CUDA ficam no processador, sem baixar o modelo grande à toa."""
    try:
        if ctranslate2.get_cuda_device_count() <= 0:
            return False
        if "float16" not in ctranslate2.get_supported_compute_types("cuda"):
            return False
    except Exception:
        return False
    if sys.platform == "win32":
        import ctypes
        for dll in ("cublas64_12.dll", "cudnn_ops64_9.dll"):
            try:
                ctypes.WinDLL(dll)
            except OSError:
                return False
    return True


class Transcritor:
    """Carrega o Whisper em segundo plano e transcreve trechos curtos."""

    def __init__(self, modelo: str = "auto", idioma: str = "pt", vocabulario: str = "",
                 ao_status: StatusFn | None = None):
        self.modelo_pedido = modelo
        self.idioma = idioma or "pt"
        self.vocabulario = vocabulario
        self.ao_status = ao_status or (lambda *a, **k: None)
        self.pronto = threading.Event()
        self.erro = ""
        self.descricao = ""
        self.dispositivo = ""
        self.segundos_por_trecho = 0.0
        self._modelo = None

    def carregar(self) -> None:
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        try:
            from faster_whisper import WhisperModel
            import ctranslate2
        except Exception as erro:
            self._falhou(f"Não consegui iniciar o Whisper: {erro}")
            return

        tem_placa = placa_de_video_utilizavel(ctranslate2)
        tentativas = []
        if tem_placa:
            nome = "large-v3-turbo" if self.modelo_pedido == "auto" else self.modelo_pedido
            tentativas.append((nome, "cuda", "float16"))
        nome_cpu = "small" if self.modelo_pedido == "auto" else self.modelo_pedido
        tentativas.append((nome_cpu, "cpu", "int8"))

        ultimo_erro = None
        for nome, dispositivo, tipo in tentativas:
            onde = "placa de vídeo" if dispositivo == "cuda" else "processador"
            try:
                caminho = self._garantir_modelo(nome)
                self.ao_status(f"Carregando o Whisper {nome} no {onde}...")
                fios = max(1, (os.cpu_count() or 4) // 2) if dispositivo == "cpu" else 0
                modelo = WhisperModel(caminho, device=dispositivo, compute_type=tipo, cpu_threads=fios)
                inicio = time.perf_counter()
                segmentos, _ = modelo.transcribe(
                    np.zeros(TAXA, dtype=np.float32), language=self.idioma, beam_size=1,
                    without_timestamps=True, vad_filter=False,
                )
                list(segmentos)
                self.segundos_por_trecho = time.perf_counter() - inicio
            except Exception as erro:
                log.warning("Whisper %s em %s falhou: %s", nome, dispositivo, erro)
                ultimo_erro = erro
                continue
            self._modelo = modelo
            self.dispositivo = dispositivo
            self.descricao = f"Whisper {nome} ({onde})"
            self.erro = ""
            self.pronto.set()
            self.ao_status(
                f"{self.descricao} pronto. Cada trecho leva cerca de "
                f"{self.segundos_por_trecho:.1f} s para transcrever.",
                pronto=True,
            )
            return
        self._falhou(f"Não consegui carregar o Whisper: {ultimo_erro}")

    def _garantir_modelo(self, nome: str) -> str:
        """Devolve a pasta do modelo, baixando na primeira vez. O faster-whisper esconde o
        progresso do download, então o status mostra pelo menos há quanto tempo está baixando."""
        from faster_whisper.utils import download_model

        try:
            return download_model(nome, local_files_only=True)
        except Exception:
            pass
        tamanho = TAMANHOS.get(nome, "")
        inicio = time.monotonic()
        terminou = threading.Event()

        def relogio():
            while True:
                s = int(time.monotonic() - inicio)
                self.ao_status(f"Baixando o Whisper {nome} ({tamanho}, só na primeira vez). "
                               f"Já são {s // 60} min {s % 60:02d} s; pode levar alguns minutos.")
                if terminou.wait(5):
                    return

        threading.Thread(target=relogio, name="relogio-download", daemon=True).start()
        try:
            return download_model(nome)
        except Exception as erro:
            raise RuntimeError(f"falha ao baixar o modelo {nome} (confira a internet): {erro}") from erro
        finally:
            terminou.set()

    def _falhou(self, mensagem: str) -> None:
        self.erro = mensagem
        log.error(mensagem)
        self.ao_status(mensagem, erro=True)

    def transcrever(self, audio: np.ndarray) -> str:
        while not self.pronto.wait(timeout=0.5):  # ainda carregando (ou baixando) o modelo
            if self.erro:
                return ""
        if self._modelo is None:
            return ""
        segmentos, _ = self._modelo.transcribe(
            audio,
            language=self.idioma,
            task="transcribe",
            beam_size=1,
            best_of=1,
            temperature=0.0,
            condition_on_previous_text=False,
            without_timestamps=True,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            hotwords=self.vocabulario.strip() or None,
            no_speech_threshold=0.6,
            log_prob_threshold=-1.0,
            compression_ratio_threshold=2.4,
        )
        partes = []
        for seg in segmentos:
            if seg.no_speech_prob > 0.6 and seg.avg_logprob < -0.8:
                continue
            if seg.compression_ratio > 2.6:  # repetição em laço
                continue
            partes.append(seg.text.strip())
        return limpar_texto(" ".join(partes), self.vocabulario)


class TranscritorDeTeste:
    """Usado só nos testes automáticos: não precisa do modelo."""

    atraso_s = 0.05
    chamadas: list[float] = []  # duração do áudio de cada chamada

    def __init__(self, *args, ao_status: StatusFn | None = None, **kwargs):
        self.ao_status = ao_status or (lambda *a, **k: None)
        self.pronto = threading.Event()
        self.erro = ""
        self.descricao = "Transcritor de teste"
        self.dispositivo = "teste"
        self.segundos_por_trecho = 0.05
        self.idioma = "pt"
        self.vocabulario = ""
        self._contador = 0
        arquivo = os.environ.get("ASSISTENTE_FRASES_TESTE")  # frases fixas para demonstração
        self._frases = []
        if arquivo and os.path.exists(arquivo):
            with open(arquivo, encoding="utf-8") as f:
                self._frases = [l.strip() for l in f if l.strip()]

    def carregar(self) -> None:
        self.pronto.set()
        self.ao_status("Transcritor de teste pronto.", pronto=True)

    def transcrever(self, audio: np.ndarray) -> str:
        time.sleep(self.atraso_s)
        TranscritorDeTeste.chamadas.append(len(audio) / TAXA)
        self._contador += 1
        if self._frases:
            return self._frases[(self._contador - 1) % len(self._frases)]
        segundos = len(audio) / TAXA
        # como no Whisper de verdade, mais áudio dá mais texto (cerca de 2 palavras por segundo)
        return f"Trecho {self._contador} com {segundos:.1f} segundos de fala" + " palavra" * int(segundos * 2) + "."
