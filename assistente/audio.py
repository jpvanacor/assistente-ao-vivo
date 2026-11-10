"""Fontes de áudio: microfone, áudio do computador (o que sai nos alto-falantes) e
arquivo (para testar com uma gravação). Todas entregam blocos float32 mono a 16 kHz
por meio de entregar(origem, bloco)."""
from __future__ import annotations

import logging
import sys
import threading
import time
import warnings
from pathlib import Path
from typing import Callable

import numpy as np

TAXA = 16000
AMOSTRAS_BLOCO = 1600  # 100 ms

log = logging.getLogger(__name__)

Entregar = Callable[[str, np.ndarray], None]


class Reamostrador:
    """Converte de qualquer taxa para 16 kHz, bloco a bloco, sem emendas audíveis.

    Usa média móvel como filtro passa-baixa simples e interpolação linear. Basta para fala.
    """

    def __init__(self, taxa_entrada: float, taxa_saida: int = TAXA):
        self.passo = float(taxa_entrada) / float(taxa_saida)
        self.k = max(1, int(round(self.passo)))
        self._hist = np.zeros(self.k - 1, dtype=np.float32)
        self._filtrado = np.zeros(0, dtype=np.float32)
        self._pos = 0.0
        self._nucleo = np.ones(self.k, dtype=np.float32) / self.k

    def __call__(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float32).reshape(-1)
        if self.passo == 1.0:
            return x
        bruto = np.concatenate([self._hist, x])
        if self.k > 1:
            self._hist = bruto[-(self.k - 1):].copy()
            filtrado = np.convolve(bruto, self._nucleo, mode="valid").astype(np.float32)
        else:
            filtrado = x
        buf = np.concatenate([self._filtrado, filtrado])
        if len(buf) < 2:
            self._filtrado = buf
            return np.zeros(0, dtype=np.float32)
        # posições de saída ainda interpoláveis (precisam de uma amostra à frente)
        n = int(np.floor((len(buf) - 1 - self._pos) / self.passo))
        if n <= 0:
            self._filtrado = buf
            return np.zeros(0, dtype=np.float32)
        t = self._pos + np.arange(n) * self.passo
        saida = np.interp(t, np.arange(len(buf)), buf).astype(np.float32)
        proximo = self._pos + n * self.passo
        descartar = int(np.floor(proximo))
        self._filtrado = buf[descartar:].copy()
        self._pos = proximo - descartar
        return saida


def ler_audio(caminho: str | Path, taxa: int = TAXA) -> np.ndarray:
    """Lê qualquer áudio ou vídeo (mp3, m4a, wav, ogg, mp4...) como float32 mono na taxa pedida.

    Não usa o decode_audio do faster-whisper, que quebrou com o PyAV 19.
    """
    import av

    partes = []
    with av.open(str(caminho), mode="r") as container:
        stream = next((s for s in container.streams if s.type == "audio"), None)
        if stream is None:
            raise RuntimeError("O arquivo não tem áudio.")
        stream.thread_type = "AUTO"
        conversor = av.AudioResampler(format="s16", layout="mono", rate=taxa)
        for pacote in container.demux(stream):
            try:
                quadros = pacote.decode()
            except av.error.InvalidDataError:
                continue  # trecho corrompido: pula
            for quadro in quadros:
                for convertido in conversor.resample(quadro):
                    partes.append(convertido.to_ndarray().reshape(-1))
        for convertido in conversor.resample(None):
            partes.append(convertido.to_ndarray().reshape(-1))
    if not partes:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(partes).astype(np.float32) / 32768.0


def listar_microfones() -> dict:
    """Microfones do sistema de áudio padrão (no Windows, o MME), sem repetições."""
    try:
        import sounddevice as sd
    except Exception as erro:  # biblioteca ausente ou sem PortAudio
        return {"microfones": [], "erro": f"Não consegui ler os microfones: {erro}"}
    try:
        padrao = sd.query_devices(kind="input")
    except Exception:
        padrao = None
    try:
        todos = sd.query_devices()
    except Exception as erro:
        return {"microfones": [], "erro": f"Não consegui ler os microfones: {erro}"}
    api = padrao["hostapi"] if padrao else 0
    microfones = []
    vistos = set()
    for indice, disp in enumerate(todos):
        if disp["max_input_channels"] < 1 or disp["hostapi"] != api:
            continue
        nome = disp["name"].strip()
        if nome in vistos:
            continue
        vistos.add(nome)
        microfones.append({
            "indice": indice,
            "nome": nome,
            "padrao": bool(padrao) and indice == padrao.get("index"),
        })
    return {"microfones": microfones, "erro": ""}


def computador_disponivel() -> bool:
    return sys.platform == "win32"


class FonteMicrofone:
    def __init__(self, entregar: Entregar, dispositivo: int | None = None, origem: str = "mic"):
        self.entregar = entregar
        self.dispositivo = dispositivo
        self.origem = origem
        self._stream = None
        self._reamostrar: Reamostrador | None = None

    def _callback(self, indata, frames, tempo, status):  # thread do PortAudio: rápido
        dados = indata[:, 0].copy()
        if self._reamostrar is not None:
            dados = self._reamostrar(dados)
        if len(dados):
            self.entregar(self.origem, dados)

    def iniciar(self) -> None:
        import sounddevice as sd

        try:
            self._reamostrar = None
            self._stream = sd.InputStream(
                samplerate=TAXA, channels=1, dtype="float32", blocksize=AMOSTRAS_BLOCO,
                device=self.dispositivo, callback=self._callback,
            )
        except Exception as primeiro_erro:
            log.info("Microfone não abriu a 16 kHz (%s); usando a taxa do aparelho.", primeiro_erro)
            try:
                info = sd.query_devices(self.dispositivo, "input")
                taxa = float(info["default_samplerate"])
                self._reamostrar = Reamostrador(taxa)
                self._stream = sd.InputStream(
                    samplerate=taxa, channels=1, dtype="float32", blocksize=int(taxa / 10),
                    device=self.dispositivo, callback=self._callback,
                )
            except Exception as erro:
                raise RuntimeError(f"Não consegui abrir o microfone: {erro}") from erro
        self._stream.start()

    def parar(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            finally:
                self._stream = None


class FonteComputador:
    """Grava o que o computador está tocando (reunião on-line), via WASAPI loopback."""

    def __init__(self, entregar: Entregar, origem: str = "pc", ao_erro: Callable[[str], None] | None = None):
        self.entregar = entregar
        self.origem = origem
        self.ao_erro = ao_erro
        self._parar = threading.Event()
        self._aberto = threading.Event()
        self._erro = ""
        self._thread: threading.Thread | None = None

    def iniciar(self) -> None:
        if not computador_disponivel():
            raise RuntimeError("Gravar o áudio do computador só funciona no Windows.")
        self._parar.clear()
        self._aberto.clear()
        self._erro = ""
        self._thread = threading.Thread(target=self._rodar, name="audio-computador", daemon=True)
        self._thread.start()
        self._aberto.wait(timeout=8)
        if self._erro:
            raise RuntimeError(self._erro)

    def _rodar(self) -> None:
        try:  # o soundcard usa COM; cada thread precisa iniciar o COM
            import ctypes
            ctypes.windll.ole32.CoInitializeEx(None, 0)
        except Exception:
            pass
        try:
            warnings.filterwarnings("ignore", message=".*discontinuity.*")
            import soundcard as sc

            alto_falante = sc.default_speaker()
            captura = sc.get_microphone(id=str(alto_falante.name), include_loopback=True)
            with captura.recorder(samplerate=TAXA, channels=1, blocksize=AMOSTRAS_BLOCO) as gravador:
                self._aberto.set()
                while not self._parar.is_set():
                    dados = gravador.record(numframes=AMOSTRAS_BLOCO)
                    self.entregar(self.origem, np.asarray(dados[:, 0], dtype=np.float32))
        except Exception as erro:
            self._erro = f"Não consegui gravar o áudio do computador: {erro}"
            log.exception("Falha no áudio do computador")
            if self._aberto.is_set() and self.ao_erro:
                self.ao_erro(self._erro)
            self._aberto.set()

    def parar(self) -> None:
        self._parar.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None


class FonteArquivo:
    """Toca um arquivo de áudio ou vídeo no ritmo real, como se fosse ao vivo."""

    def __init__(self, caminho: Path, entregar: Entregar, origem: str = "arquivo",
                 velocidade: float = 1.0, ao_terminar: Callable[[], None] | None = None):
        self.caminho = Path(caminho)
        self.entregar = entregar
        self.origem = origem
        self.velocidade = max(0.25, float(velocidade))
        self.ao_terminar = ao_terminar
        self._parar = threading.Event()
        self._thread: threading.Thread | None = None
        self.duracao_s = 0.0

    def iniciar(self) -> None:
        try:
            self._audio = ler_audio(self.caminho)
        except Exception as erro:
            raise RuntimeError(f"Não consegui ler o arquivo de áudio: {erro}") from erro
        self.duracao_s = len(self._audio) / TAXA
        self._parar.clear()
        self._thread = threading.Thread(target=self._rodar, name="audio-arquivo", daemon=True)
        self._thread.start()

    def _rodar(self) -> None:
        inicio = time.monotonic()
        audio = self._audio
        for i in range(0, len(audio), AMOSTRAS_BLOCO):
            if self._parar.is_set():
                break
            alvo = inicio + (i / TAXA) / self.velocidade
            espera = alvo - time.monotonic()
            if espera > 0:
                time.sleep(espera)
            self.entregar(self.origem, audio[i:i + AMOSTRAS_BLOCO])
        if not self._parar.is_set():
            # um segundo de silêncio fecha o último trecho de fala
            for _ in range(10):
                self.entregar(self.origem, np.zeros(AMOSTRAS_BLOCO, dtype=np.float32))
            if self.ao_terminar:
                self.ao_terminar()

    def parar(self) -> None:
        self._parar.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        self._thread = None
