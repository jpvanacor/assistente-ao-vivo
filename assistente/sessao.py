"""Estado da conversa (transcrição e sugestões) e gravação em Markdown na pasta das sessões."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .eventos import Barramento

log = logging.getLogger(__name__)

ROTULOS_ORIGEM = {
    "mic": "Microfone",
    "pc": "Computador",
    "navegador": "Microfone",
    "arquivo": "Arquivo",
}
TIPOS_SUGESTAO = {"auto": "Sugestão automática", "agora": "Sugestão pedida", "pergunta": "Pergunta"}


@dataclass
class Linha:
    id: int
    hora: str
    origem: str
    texto: str
    t: float = field(default_factory=time.time)


@dataclass
class Sugestao:
    id: int
    hora: str
    tipo: str
    pergunta: str = ""
    texto: str = ""
    modelo: str = ""
    estado: str = "andamento"  # andamento | pronta | vazia | erro
    custo: float = 0.0
    latencia_s: float | None = None
    erro: str = ""


class Sessao:
    def __init__(self, pasta: Path, barramento: Barramento):
        self._trava = threading.RLock()
        self.barramento = barramento
        self.pasta = Path(pasta)
        self.descricao = ""  # cabeçalho do arquivo (modo, modelos)
        self.contexto = ""
        self._iniciar_vazia()

    def _iniciar_vazia(self) -> None:
        self.linhas: list[Linha] = []
        self.sugestoes: list[Sugestao] = []
        self.custo_total = 0.0
        self.caracteres = 0
        self.caracteres_na_ultima = 0
        self.inicio = datetime.now()
        self.arquivo: Path | None = None
        self._proximo_id = 1

    def nova(self) -> None:
        with self._trava:
            self._iniciar_vazia()
        self.barramento.publicar("sessao_nova")

    def _id(self) -> int:
        valor = self._proximo_id
        self._proximo_id += 1
        return valor

    # --- transcrição -------------------------------------------------------------
    def adicionar_linha(self, origem: str, texto: str, momento: float | None = None) -> Linha:
        momento = momento or time.time()
        with self._trava:
            linha = Linha(self._id(), datetime.fromtimestamp(momento).strftime("%H:%M:%S"), origem, texto, momento)
            self.linhas.append(linha)
            self.caracteres += len(texto)
            self._gravar(f"[{linha.hora}] ({ROTULOS_ORIGEM.get(origem, origem)}) {texto}")
        self.barramento.publicar("linha", linha=asdict(linha))
        return linha

    def caracteres_novos(self) -> int:
        with self._trava:
            return self.caracteres - self.caracteres_na_ultima

    def copia_linhas(self) -> list[Linha]:
        with self._trava:
            return list(self.linhas)

    def ultimas_sugestoes(self, n: int = 5) -> list[str]:
        with self._trava:
            prontas = [s.texto for s in self.sugestoes if s.estado == "pronta" and s.tipo != "pergunta"]
        return prontas[-n:]

    # --- sugestões ---------------------------------------------------------------
    def nova_sugestao(self, tipo: str, pergunta: str, modelo: str) -> int:
        with self._trava:
            sugestao = Sugestao(self._id(), datetime.now().strftime("%H:%M:%S"), tipo, pergunta, modelo=modelo)
            self.sugestoes.append(sugestao)
            self.caracteres_na_ultima = self.caracteres
        self.barramento.publicar("sugestao_inicio", sugestao=asdict(sugestao))
        return sugestao.id

    def _achar(self, sid: int) -> Sugestao | None:
        for s in reversed(self.sugestoes):
            if s.id == sid:
                return s
        return None

    def trecho_sugestao(self, sid: int, trecho: str) -> None:
        with self._trava:
            s = self._achar(sid)
            if s is None:
                return
            s.texto += trecho
        self.barramento.publicar("sugestao_trecho", id=sid, trecho=trecho)

    def concluir_sugestao(self, sid: int, texto: str, custo: float, latencia_s: float | None,
                          vazia: bool = False) -> None:
        with self._trava:
            s = self._achar(sid)
            if s is None:
                return
            s.texto = "" if vazia else texto.strip()
            s.estado = "vazia" if vazia else "pronta"
            s.custo = custo
            s.latencia_s = latencia_s
            self.custo_total += custo
            if not vazia:
                rotulo = TIPOS_SUGESTAO.get(s.tipo, s.tipo)
                if s.tipo == "pergunta":
                    bloco = f"> [{s.hora}] Pergunta: {s.pergunta}\n> Resposta:\n" + _citar(s.texto)
                else:
                    bloco = f"> [{s.hora}] {rotulo}:\n" + _citar(s.texto)
                self._gravar(bloco)
            total = self.custo_total
        self.barramento.publicar("sugestao_fim", id=sid, texto=s.texto, estado=s.estado, custo=custo,
                                 latencia_s=latencia_s, custo_total=total)

    def erro_sugestao(self, sid: int, mensagem: str) -> None:
        with self._trava:
            s = self._achar(sid)
            if s is not None:
                s.estado = "erro"
                s.erro = mensagem
        self.barramento.publicar("sugestao_erro", id=sid, erro=mensagem)

    # --- arquivo -----------------------------------------------------------------
    def _gravar(self, texto: str) -> None:
        try:
            if self.arquivo is None:
                self.pasta.mkdir(parents=True, exist_ok=True)
                nome = self.inicio.strftime("%Y-%m-%d %Hh%M") + " - sessao.md"
                self.arquivo = self.pasta / nome
                cabecalho = f"# Sessão de {self.inicio.strftime('%d/%m/%Y, %Hh%M')}\n\n"
                if self.descricao:
                    cabecalho += self.descricao + "\n\n"
                if self.contexto.strip():
                    cabecalho += "## Contexto\n\n" + self.contexto.strip() + "\n\n"
                cabecalho += "## Transcrição e sugestões\n\n"
                cabecalho += ("A transcrição é automática e pode ter erros. "
                              "As sugestões são do Claude e precisam de conferência.\n\n")
                self.arquivo.write_text(cabecalho, encoding="utf-8")
            with self.arquivo.open("a", encoding="utf-8") as f:
                f.write(texto.rstrip() + "\n\n")
        except OSError as erro:
            log.warning("Não consegui gravar a sessão: %s", erro)

    def estado(self) -> dict:
        with self._trava:
            return {
                "linhas": [asdict(l) for l in self.linhas[-400:]],
                "sugestoes": [asdict(s) for s in self.sugestoes[-40:]],
                "custo_total": self.custo_total,
                "arquivo": str(self.arquivo) if self.arquivo else "",
            }


def _citar(texto: str) -> str:
    return "\n".join("> " + l if l.strip() else ">" for l in texto.strip().splitlines())
