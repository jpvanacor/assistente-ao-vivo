"""Sugestões do Claude a partir da transcrição, em fluxo (o texto aparece enquanto é escrito)."""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass

from .sessao import Linha, ROTULOS_ORIGEM

log = logging.getLogger(__name__)

# Preços em US$ por milhão de tokens (platform.claude.com/docs/en/about-claude/pricing, nov/2026).
MODELOS_IA = {
    "claude-haiku-5-5": {
        "nome": "Claude Haiku 5.5 (mais rápido)",
        "thinking": {"type": "disabled"},
        "effort": "low",
        "preco": {"entrada": 0.10, "escrita_cache": 0.125, "leitura_cache": 0.01, "saida": 0.50},
    },
    "claude-sonnet-5-5": {
        "nome": "Claude Sonnet 5.5 (mais cuidadoso, um pouco mais lento)",
        "thinking": {"type": "between_tools"},
        "effort": "low",
        "preco": {"entrada": 2.00, "escrita_cache": 2.50, "leitura_cache": 0.10, "saida": 10.00},
    },
}

SISTEMA = """Você acompanha, em tempo real, uma conversa de trabalho de um assessor jurídico da 3ª Promotoria de Justiça Cível de São Leopoldo, do Ministério Público do Rio Grande do Sul. A transcrição automática da conversa chega aos poucos. Seu papel é ajudar o assessor durante a conversa, com sugestões que ele consegue ler de relance.

Formato das sugestões:
- No máximo 3 itens, cada um numa linha que começa com "- " e tem até 25 palavras.
- Comece cada item com um destes rótulos: "Pergunte:", "Diga:", "Atenção:", "Confira:", "Anote:" ou "Base legal:".
- Concentre-se nas falas mais recentes e no que o assessor pode fazer agora.
- Não repita sugestões já dadas.
- Se não houver nada útil a acrescentar, responda apenas SEM_SUGESTAO.
- Não use títulos, negrito nem travessão.

Quando o assessor fizer uma pergunta direta, responda a ela de forma curta e direta; use os rótulos só se ajudarem.

Regras de honestidade:
- Use só a transcrição e o contexto informado. Não invente fatos, nomes, datas, números ou documentos.
- Ao citar lei, artigo, súmula ou jurisprudência de memória, comece o item com [Não verificado].
- Ao concluir algo que ninguém disse, comece o item com [Inferência].
- A transcrição não identifica quem fala e pode ter erros. Se um trecho parecer erro de transcrição, avise em vez de tirar conclusões dele."""

FOCO = {
    "audiencia": ("Tipo de conversa: audiência ou oitiva. Ajude a apontar contradições com falas anteriores "
                  "ou com o contexto, perguntas de seguimento para esclarecer fatos, datas, valores e pessoas "
                  "citadas, e o que convém constar em ata."),
    "reuniao": ("Tipo de conversa: reunião ou atendimento. Ajude com perguntas para esclarecer os fatos, "
                "documentos e informações a pedir, prazos e compromissos a registrar e encaminhamentos possíveis."),
    "livre": "Tipo de conversa: livre. Ajude no que for mais útil no momento.",
}

NOTA_DUAS_FONTES = ("As linhas marcadas (Microfone) vêm do microfone do assessor, isto é, dele e de quem está "
                    "na sala com ele; as marcadas (Computador) vêm do áudio do computador, isto é, das outras "
                    "pessoas numa reunião on-line.")

CABECALHO_TRANSCRICAO = "Transcrição da conversa até agora (automática, pode ter erros):"
SEM_SUGESTAO = "SEM_SUGESTAO"
LIMITE_CARACTERES = 60_000
LINHAS_POR_BLOCO = 10
CACHE = {"type": "ephemeral"}


@dataclass
class Pedido:
    tipo: str  # auto | agora | pergunta
    pergunta: str = ""


def montar_pedido(linhas: list[Linha], contexto: str, modo: str, pedido: Pedido,
                  anteriores: list[str], duas_fontes: bool) -> tuple[list, list]:
    """Monta system e messages. O começo do pedido não muda entre chamadas (instruções,
    contexto, blocos já completos da transcrição), para aproveitar o cache de prompt."""
    texto_sistema = SISTEMA + "\n\n" + FOCO.get(modo, FOCO["livre"])
    if duas_fontes:
        texto_sistema += "\n\n" + NOTA_DUAS_FONTES
    system = [{"type": "text", "text": texto_sistema}]
    if contexto.strip():
        system.append({"type": "text",
                       "text": "Contexto do caso, informado pelo assessor antes da conversa:\n\n" + contexto.strip()})
    system[-1]["cache_control"] = CACHE

    textos = []
    for linha in linhas:
        rotulo = f" ({ROTULOS_ORIGEM.get(linha.origem, linha.origem)})" if duas_fontes else ""
        textos.append(f"[{linha.hora}]{rotulo} {linha.texto}")
    # Corta o começo em múltiplos do bloco, para os blocos seguintes continuarem iguais.
    inicio = 0
    while sum(len(t) + 1 for t in textos[inicio:]) > LIMITE_CARACTERES and inicio < len(textos):
        inicio += LINHAS_POR_BLOCO

    conteudo: list[dict] = [{"type": "text", "text": CABECALHO_TRANSCRICAO}]
    if inicio:
        conteudo[0]["text"] += "\n(O início da conversa foi omitido por ser longo.)"
    blocos = [textos[i:i + LINHAS_POR_BLOCO] for i in range(inicio, len(textos), LINHAS_POR_BLOCO)]
    if not blocos:
        conteudo.append({"type": "text", "text": "(Ainda não há falas transcritas.)"})
    completos = [b for b in blocos if len(b) == LINHAS_POR_BLOCO]
    for bloco in blocos:
        conteudo.append({"type": "text", "text": "\n".join(bloco)})
    if completos:
        conteudo[1 + len(completos) - 1]["cache_control"] = CACHE

    if pedido.tipo == "pergunta":
        instrucao = (f"Pergunta do assessor: {pedido.pergunta.strip()}\n\n"
                     "Responda de forma direta, em até 80 palavras, usando a transcrição e o contexto quando ajudarem.")
    else:
        ja_dadas = "\n".join(t.strip()[:400] for t in anteriores) or "(nenhuma)"
        instrucao = f"Sugestões já dadas nesta conversa (não repita):\n{ja_dadas}\n\n"
        if pedido.tipo == "agora":
            instrucao += "O assessor pediu ajuda agora. Dê as sugestões mais úteis para este momento da conversa."
        else:
            instrucao += ("Dê as sugestões para este momento da conversa, com foco nas últimas falas. "
                          f"Se não houver nada útil a acrescentar, responda apenas {SEM_SUGESTAO}.")
    conteudo.append({"type": "text", "text": instrucao})
    return system, [{"role": "user", "content": conteudo}]


def custo_em_dolares(uso, preco: dict) -> float:
    def n(nome):
        return getattr(uso, nome, 0) or 0
    return (n("input_tokens") * preco["entrada"]
            + n("cache_creation_input_tokens") * preco["escrita_cache"]
            + n("cache_read_input_tokens") * preco["leitura_cache"]
            + n("output_tokens") * preco["saida"]) / 1_000_000


def mensagem_de_erro(erro: Exception) -> str:
    import anthropic

    if isinstance(erro, anthropic.AuthenticationError):
        return "A chave da API foi recusada. Confira a chave em Ajustes."
    if isinstance(erro, anthropic.PermissionDeniedError):
        return "A chave não tem permissão para usar este modelo."
    if isinstance(erro, anthropic.RateLimitError):
        return "Muitos pedidos seguidos (limite da API). Tente de novo em alguns segundos."
    if isinstance(erro, anthropic.BadRequestError):
        texto = str(erro).lower()
        if "credit" in texto or "billing" in texto:
            return "A conta da API está sem créditos. Adicione créditos em platform.claude.com/settings/billing."
        if "workspace" in texto:
            return ("A API pediu o workspace. Crie a chave escolhendo um workspace "
                    "ou preencha o ID do workspace em Ajustes.")
        return f"A API recusou o pedido: {getattr(erro, 'message', erro)}"
    if isinstance(erro, anthropic.APIConnectionError):
        return "Sem conexão com a API do Claude. Confira a internet."
    if isinstance(erro, anthropic.APIStatusError):
        return f"Erro da API do Claude (código {erro.status_code}). Tente de novo."
    return f"Erro inesperado: {erro}"


class AssistenteIA:
    """Fila de pedidos ao Claude, atendidos um de cada vez em uma thread própria."""

    def __init__(self, config, sessao, ao_ocupado=None):
        self.config = config
        self.sessao = sessao
        self.ao_ocupado = ao_ocupado or (lambda ocupado: None)
        self._fila: queue.Queue[Pedido] = queue.Queue()
        self._ocupado = threading.Event()
        self._clientes: dict = {}
        self.ultimo_pedido = 0.0
        self._thread = threading.Thread(target=self._trabalhar, name="ia", daemon=True)
        self._thread.start()

    @property
    def ocupado(self) -> bool:
        return self._ocupado.is_set() or not self._fila.empty()

    def pedir(self, tipo: str, pergunta: str = "") -> bool:
        if tipo == "auto" and self.ocupado:
            return False
        self.ultimo_pedido = time.time()
        self._fila.put(Pedido(tipo, pergunta))
        return True

    def _cliente(self, chave: str):
        import anthropic

        if chave not in self._clientes:
            self._clientes.clear()
            self._clientes[chave] = anthropic.Anthropic(api_key=chave, max_retries=1, timeout=45.0)
        return self._clientes[chave]

    def _trabalhar(self) -> None:
        while True:
            pedido = self._fila.get()
            self._ocupado.set()
            self.ao_ocupado(True)
            try:
                self._atender(pedido)
            except Exception:
                log.exception("Falha inesperada ao pedir sugestão")
            finally:
                self._ocupado.clear()
                self.ao_ocupado(self.ocupado)

    def _atender(self, pedido: Pedido) -> None:
        cfg = self.config
        modelo = cfg["modelo_ia"]
        info = MODELOS_IA.get(modelo, MODELOS_IA["claude-haiku-5-5"])
        sid = self.sessao.nova_sugestao(pedido.tipo, pedido.pergunta, info["nome"])
        chave = cfg.chave_api()
        if not chave:
            self.sessao.erro_sugestao(sid, "Falta a chave da API do Claude. Cole a chave em Ajustes.")
            return

        system, messages = montar_pedido(
            self.sessao.copia_linhas(), cfg["contexto"], cfg["modo"], pedido,
            self.sessao.ultimas_sugestoes(), cfg["fonte"] == "ambos" and cfg["motor"] == "whisper",
        )
        argumentos = dict(
            model=modelo,
            max_tokens=900 if pedido.tipo == "pergunta" else 600,
            system=system,
            messages=messages,
            thinking=info["thinking"],
            output_config={"effort": info["effort"]},
        )
        if cfg["workspace_id"]:
            argumentos["workspace_id"] = cfg["workspace_id"]

        texto = ""
        mostrado = False
        segurar = pedido.tipo == "auto"  # só a sugestão automática pode vir vazia
        inicio = time.perf_counter()
        latencia = None
        try:
            with self._cliente(chave).messages.stream(**argumentos) as fluxo:
                for trecho in fluxo.text_stream:
                    if latencia is None:
                        latencia = time.perf_counter() - inicio
                    texto += trecho
                    if mostrado:
                        self.sessao.trecho_sugestao(sid, trecho)
                        continue
                    comeco = texto.lstrip().upper()
                    if segurar and (SEM_SUGESTAO.startswith(comeco) or comeco.startswith(SEM_SUGESTAO)):
                        continue  # pode ser SEM_SUGESTAO: espera mais um pouco
                    mostrado = True
                    self.sessao.trecho_sugestao(sid, texto)
                final = fluxo.get_final_message()
        except Exception as erro:
            log.warning("Pedido ao Claude falhou: %r", erro)
            self.sessao.erro_sugestao(sid, mensagem_de_erro(erro))
            return

        custo = custo_em_dolares(final.usage, info["preco"])
        vazia = segurar and texto.strip().upper().startswith(SEM_SUGESTAO)
        if not mostrado and not vazia and texto:
            self.sessao.trecho_sugestao(sid, texto)
        self.sessao.concluir_sugestao(sid, texto, custo, latencia, vazia=vazia or not texto.strip())
