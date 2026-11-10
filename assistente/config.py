"""Configurações, gravadas em config.json na pasta de dados do usuário."""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path


def pasta_dados() -> Path:
    """Pasta onde ficam config.json e as sessões gravadas."""
    personalizada = os.environ.get("ASSISTENTE_DADOS")
    if personalizada:
        return Path(personalizada)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "AssistenteAoVivo"
    return Path.home() / ".assistente-ao-vivo"


VOCABULARIO_PADRAO = (
    "Ministério Público, Promotoria de Justiça, Promotora, São Leopoldo, "
    "inquérito civil, notícia de fato, procedimento preparatório, improbidade administrativa, "
    "licitação, Defensoria Pública, eproc"
)

MODOS = ("audiencia", "reuniao", "livre")
MOTORES = ("whisper", "navegador")
MODELOS_WHISPER = ("auto", "base", "small", "medium", "large-v3-turbo")
FONTES = ("microfone", "computador", "ambos")
MODELOS_IA = ("claude-haiku-5-5", "claude-sonnet-5-5")

PADRAO: dict = {
    "api_key": "",
    "workspace_id": "",
    "modelo_ia": "claude-haiku-5-5",
    "modo": "reuniao",
    "auto_sugerir": True,
    "intervalo_s": 20,
    "contexto": "",
    "vocabulario": VOCABULARIO_PADRAO,
    "motor": "whisper",
    "modelo_whisper": "auto",
    "idioma": "pt",
    "fonte": "microfone",
    "microfone": None,
    "pasta_sessoes": "",
}


def _texto(v, limite: int) -> str:
    if v is None:
        return ""
    if not isinstance(v, str):
        raise ValueError("deve ser texto")
    return v[:limite]


def _opcao(opcoes):
    def validar(v):
        if v not in opcoes:
            raise ValueError(f"deve ser um de {', '.join(opcoes)}")
        return v
    return validar


def _microfone(v):
    if v in (None, "", "padrao"):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ValueError("deve ser o número do dispositivo") from None


def _intervalo(v):
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ValueError("deve ser um número de segundos") from None
    return max(5, min(300, n))


def _booleano(v):
    if isinstance(v, bool):
        return v
    if v in ("true", "1", 1):
        return True
    if v in ("false", "0", 0):
        return False
    raise ValueError("deve ser verdadeiro ou falso")


VALIDADORES = {
    "api_key": lambda v: _texto(v, 300).strip(),
    "workspace_id": lambda v: _texto(v, 200).strip(),
    "modelo_ia": _opcao(MODELOS_IA),
    "modo": _opcao(MODOS),
    "auto_sugerir": _booleano,
    "intervalo_s": _intervalo,
    "contexto": lambda v: _texto(v, 60_000),
    "vocabulario": lambda v: _texto(v, 1_000),
    "motor": _opcao(MOTORES),
    "modelo_whisper": _opcao(MODELOS_WHISPER),
    "idioma": lambda v: (_texto(v, 10).strip().lower() or "pt"),
    "fonte": _opcao(FONTES),
    "microfone": _microfone,
    "pasta_sessoes": lambda v: _texto(v, 500).strip(),
}


class Config:
    """Configuração com gravação imediata em disco. Segura para várias threads."""

    def __init__(self, pasta: Path | None = None):
        self.pasta = Path(pasta) if pasta else pasta_dados()
        self.pasta.mkdir(parents=True, exist_ok=True)
        self.arquivo = self.pasta / "config.json"
        self._trava = threading.Lock()
        self._dados = dict(PADRAO)
        if self.arquivo.exists():
            try:
                salvos = json.loads(self.arquivo.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                salvos = {}
            for chave, valor in salvos.items():
                if chave in VALIDADORES:
                    try:
                        self._dados[chave] = VALIDADORES[chave](valor)
                    except ValueError:
                        pass

    def __getitem__(self, chave: str):
        with self._trava:
            return self._dados[chave]

    def chave_api(self) -> str:
        return self["api_key"] or os.environ.get("ANTHROPIC_API_KEY", "")

    def atualizar(self, novos: dict) -> dict:
        """Valida e grava. Devolve só os campos que mudaram."""
        limpos = {}
        for chave, valor in novos.items():
            if chave not in VALIDADORES:
                continue
            try:
                limpos[chave] = VALIDADORES[chave](valor)
            except ValueError as erro:
                raise ValueError(f"Ajuste inválido ({chave}): {erro}") from None
        with self._trava:
            mudou = {k: v for k, v in limpos.items() if self._dados.get(k) != v}
            self._dados.update(limpos)
            self._gravar()
        return mudou

    def _gravar(self) -> None:
        temporario = self.arquivo.with_suffix(".tmp")
        temporario.write_text(json.dumps(self._dados, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporario, self.arquivo)

    def pasta_sessoes(self) -> Path:
        escolhida = self["pasta_sessoes"]
        return Path(escolhida) if escolhida else self.pasta / "sessoes"

    def publico(self) -> dict:
        """O que pode ir para a página: nunca a chave inteira."""
        with self._trava:
            dados = dict(self._dados)
        chave = dados.pop("api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
        dados["tem_chave"] = bool(chave)
        dados["chave_final"] = chave[-4:] if len(chave) > 8 else ""
        dados["pasta_sessoes_efetiva"] = str(self.pasta_sessoes())
        return dados
