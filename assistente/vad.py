"""Detecção de fala (Silero VAD, o mesmo modelo que o faster-whisper traz) e corte do
áudio contínuo em trechos de fala, prontos para transcrever."""
from __future__ import annotations

import collections
from dataclasses import dataclass

import numpy as np

TAXA = 16000
JANELA = 512  # 32 ms
CONTEXTO = 64


class VadSilero:
    """Silero VAD em fluxo: guarda o estado entre chamadas, então o resultado é o mesmo
    que analisar o áudio inteiro de uma vez."""

    def __init__(self):
        from faster_whisper.vad import get_vad_model

        self._sessao = get_vad_model().session
        self.reiniciar()

    def reiniciar(self) -> None:
        self._h = np.zeros((1, 1, 128), dtype=np.float32)
        self._c = np.zeros((1, 1, 128), dtype=np.float32)
        self._ctx = np.zeros(CONTEXTO, dtype=np.float32)

    def probabilidades(self, janelas: np.ndarray) -> np.ndarray:
        """janelas: matriz [n, 512]. Devolve a probabilidade de fala de cada janela."""
        janelas = np.asarray(janelas, dtype=np.float32)
        contextos = np.vstack([self._ctx[None, :], janelas[:-1, -CONTEXTO:]])
        entrada = np.concatenate([contextos, janelas], axis=1)
        saida, self._h, self._c = self._sessao.run(None, {"input": entrada, "h": self._h, "c": self._c})
        self._ctx = janelas[-1, -CONTEXTO:].copy()
        return np.asarray(saida, dtype=np.float32).reshape(-1)


@dataclass
class Trecho:
    audio: np.ndarray
    inicio_s: float  # segundos desde o início da escuta desta fonte

    @property
    def duracao_s(self) -> float:
        return len(self.audio) / TAXA


class Segmentador:
    """Recebe blocos de áudio e devolve trechos de fala completos.

    Um trecho termina depois de uma pausa (silencio_fim_ms) ou, em fala corrida, ao
    chegar a max_s segundos; nesse caso o corte vai para o ponto mais baixo do último terço,
    para não partir palavras.
    """

    def __init__(self, vad=None, limiar: float = 0.5, silencio_fim_ms: int = 600,
                 fala_min_ms: int = 300, max_s: float = 10.0, margem_ms: int = 250):
        self.vad = vad if vad is not None else VadSilero()
        self.limiar = limiar
        self.limiar_baixo = max(limiar - 0.15, 0.01)
        ms = JANELA * 1000 / TAXA
        self.n_silencio = max(1, round(silencio_fim_ms / ms))
        self.n_fala_min = max(1, round(fala_min_ms / ms))
        self.n_max = max(8, round(max_s * 1000 / ms))
        self.n_margem = max(1, round(margem_ms / ms))
        self._resto = np.zeros(0, dtype=np.float32)
        self._antes: collections.deque = collections.deque(maxlen=self.n_margem)
        self._janelas: list[np.ndarray] = []
        self._probs: list[float] = []
        self._em_fala = False
        self._silencio = 0
        self._indice = 0  # próxima janela (contagem global)
        self._inicio = 0  # primeira janela do trecho atual
        self.ultima_prob = 0.0

    @property
    def em_fala(self) -> bool:
        return self._em_fala

    def alimentar(self, bloco: np.ndarray) -> list[Trecho]:
        dados = np.concatenate([self._resto, np.asarray(bloco, dtype=np.float32).reshape(-1)])
        n = len(dados) // JANELA
        self._resto = dados[n * JANELA:].copy()
        if n == 0:
            return []
        janelas = dados[: n * JANELA].reshape(n, JANELA)
        probs = self.vad.probabilidades(janelas)
        prontos: list[Trecho] = []
        for janela, prob in zip(janelas, probs):
            self._passo(janela, float(prob), prontos)
        self.ultima_prob = float(probs[-1])
        return prontos

    def finalizar(self) -> list[Trecho]:
        """Fecha o trecho em andamento (ao parar de ouvir)."""
        prontos: list[Trecho] = []
        if self._em_fala:
            fim = len(self._janelas) - self._silencio + min(self._silencio, self.n_margem)
            self._emitir(fim, prontos)
        self._em_fala = False
        self._janelas, self._probs, self._silencio = [], [], 0
        self._antes.clear()
        self._resto = np.zeros(0, dtype=np.float32)
        return prontos

    def _passo(self, janela: np.ndarray, prob: float, prontos: list[Trecho]) -> None:
        indice = self._indice
        self._indice += 1
        if not self._em_fala:
            if prob >= self.limiar:
                self._em_fala = True
                self._janelas = list(self._antes) + [janela]
                self._probs = [0.0] * len(self._antes) + [prob]
                self._inicio = indice - len(self._antes)
                self._antes.clear()
                self._silencio = 0
            else:
                self._antes.append(janela)
            return

        self._janelas.append(janela)
        self._probs.append(prob)
        self._silencio = self._silencio + 1 if prob < self.limiar_baixo else 0

        if self._silencio >= self.n_silencio:
            fim = len(self._janelas) - self._silencio + self.n_margem
            self._emitir(fim, prontos)
            sobra = self._janelas[fim:]
            self._em_fala = False
            self._janelas, self._probs, self._silencio = [], [], 0
            self._antes.clear()
            self._antes.extend(sobra[-self.n_margem:])
        elif len(self._janelas) >= self.n_max:
            busca = len(self._janelas) * 2 // 3
            corte = busca + int(np.argmin(self._probs[busca:])) + 1
            self._emitir(corte, prontos)
            self._inicio += corte
            self._janelas = self._janelas[corte:]
            self._probs = self._probs[corte:]
            self._silencio = 0
            for p in reversed(self._probs):
                if p >= self.limiar_baixo:
                    break
                self._silencio += 1

    def _emitir(self, fim: int, prontos: list[Trecho]) -> None:
        fim = max(0, min(fim, len(self._janelas)))
        if fim == 0:
            return
        falas = sum(1 for p in self._probs[:fim] if p >= self.limiar)
        if falas < self.n_fala_min:
            return
        audio = np.concatenate(self._janelas[:fim])
        prontos.append(Trecho(audio=audio, inicio_s=self._inicio * JANELA / TAXA))
