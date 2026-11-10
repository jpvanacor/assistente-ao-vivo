"""Teste visual da página num Chrome sem janela (não roda no pytest).

Uso, na raiz do projeto:  python tests/teste_pagina.py /caminho/do/chrome pasta_de_saida
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from playwright.sync_api import expect, sync_playwright  # noqa: E402

from tests.mock_anthropic import MockAnthropic  # noqa: E402

FRASES = [
    "Bom dia, obrigado por virem. Hoje a ideia é entender como foi feita a contratação da empresa de limpeza urbana.",
    "O contrato foi assinado em março, depois de um pregão eletrônico com três empresas participantes.",
    "A empresa vencedora já prestava o serviço antes, por contratação emergencial.",
    "Os pagamentos de abril e maio foram maiores porque houve um aditivo de vinte e cinco por cento.",
    "O fiscal do contrato é o servidor Carlos, da Secretaria de Obras.",
    "Nós podemos encaminhar as notas fiscais e os relatórios de medição em quinze dias.",
    "A emergencial durou uns oito meses, até sair o pregão.",
]
SUGESTAO = (
    "- Pergunte: quem autorizou o aditivo de 25% e qual foi a justificativa técnica?\n"
    "- Confira: se a emergencial de oito meses foi com a mesma empresa e se houve prorrogação.\n"
    "- Anote: o compromisso de enviar notas fiscais e medições em 15 dias."
)
RESPOSTA = (
    "[Não verificado] Base legal: na Lei 14.133/2021, os acréscimos em serviços ficam limitados a 25% do valor "
    "inicial atualizado (art. 125). Vale pedir o termo aditivo e o parecer jurídico que o embasou."
)


def main(chrome: str, saida: Path) -> int:
    saida.mkdir(parents=True, exist_ok=True)
    api = MockAnthropic().iniciar()
    api.resposta_padrao = SUGESTAO
    api.resposta_pergunta = RESPOSTA
    api.atraso_s = 0.04
    tmp = Path(tempfile.mkdtemp())
    frases = tmp / "frases.txt"
    frases.write_text("\n".join(FRASES), encoding="utf-8")
    porta = 8799
    env = dict(os.environ, ANTHROPIC_BASE_URL=api.url, ASSISTENTE_FRASES_TESTE=str(frases))
    env.pop("ANTHROPIC_API_KEY", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "assistente", "--porta", str(porta), "--dados", str(tmp / "dados"),
         "--sem-navegador", "--transcritor-de-teste"],
        cwd=RAIZ, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    base = f"http://127.0.0.1:{porta}"
    for _ in range(100):
        if proc.poll() is not None:
            raise SystemExit("o programa fechou ao iniciar:\n" + proc.stdout.read())
        try:
            urllib.request.urlopen(base + "/api/estado", timeout=1)
            break
        except Exception:
            time.sleep(0.2)
    else:
        raise SystemExit("o programa não respondeu em 20 s")
    erros: list[str] = []
    try:
        with sync_playwright() as p:
            navegador = p.chromium.launch(executable_path=chrome, headless=True)
            pagina = navegador.new_page(viewport={"width": 1366, "height": 768})
            pagina.on("pageerror", lambda e: erros.append(f"erro de JS: {e}"))
            pagina.on("console", lambda m: m.type == "error" and erros.append(f"console: {m.text}"))
            pagina.on("dialog", lambda d: d.accept())
            pagina.goto(base)
            expect(pagina.locator("#status")).to_contain_text("pronto", timeout=10000)
            expect(pagina.locator("#faixa")).to_be_visible()
            pagina.screenshot(path=str(saida / "1-inicio.png"))

            # chave da API
            pagina.click("#btnAjustes")
            pagina.fill("#campoChave", "sk-ant-api03-teste-ABCD")
            pagina.click("#btnSalvarChave")
            expect(pagina.locator("#avisos")).to_contain_text("Chave salva")
            expect(pagina.locator("#ajudaChave")).to_contain_text("termina em ABCD")
            pagina.screenshot(path=str(saida / "2-ajustes.png"))
            expect(pagina.locator("#faixa")).to_be_hidden()

            # toca uma gravação como se fosse ao vivo
            pagina.set_input_files("#arquivoTeste", str(RAIZ / "tests" / "_dados" / "physicsworks.wav"))
            pagina.select_option("#selVelocidade", "2")
            pagina.click("#btnTocarArquivo")
            expect(pagina.locator("#btnOuvir")).to_contain_text("Parar teste", timeout=15000)
            expect(pagina.locator("#listaTranscricao .linha").nth(5)).to_be_visible(timeout=40000)
            expect(pagina.locator("#niveis .medidor")).to_have_count(1)

            # F2 pede sugestões; a resposta chega em fluxo
            pagina.keyboard.press("F2")
            expect(pagina.locator(".cartao.pronta").first).to_contain_text("quem autorizou o aditivo", timeout=15000)
            expect(pagina.locator(".cartao.pronta .rotulo").first).to_have_text("Pergunte:")

            # pergunta direta
            pagina.keyboard.press("/")
            pagina.keyboard.type("Qual é o limite legal para aditivo?")
            pagina.keyboard.press("Enter")
            expect(pagina.locator(".cartao.pronta").first).to_contain_text("14.133", timeout=15000)
            expect(pagina.locator(".cartao.pronta .marca-honestidade").first).to_have_text("Não verificado")
            time.sleep(0.5)
            pagina.screenshot(path=str(saida / "3-conversa.png"))

            # contexto
            pagina.click("#btnContexto")
            pagina.fill("#txtContexto", "NF sobre contratação emergencial de limpeza urbana; reunião com a Secretaria.")
            pagina.select_option("#selModo", "reuniao")
            pagina.click("#btnSalvarContexto")
            expect(pagina.locator("#contextoSalvo")).to_have_text("Salvo.")
            pagina.keyboard.press("Escape")

            pagina.click("#btnOuvir")  # para o teste
            expect(pagina.locator("#btnOuvir")).to_contain_text("Ouvir", timeout=10000)

            # tema escuro e tela estreita
            escura = navegador.new_page(viewport={"width": 420, "height": 900}, color_scheme="dark")
            escura.on("pageerror", lambda e: erros.append(f"erro de JS (escura): {e}"))
            escura.goto(base)
            expect(escura.locator(".cartao").first).to_be_visible(timeout=10000)
            escura.screenshot(path=str(saida / "4-escuro-estreito.png"), full_page=True)
            navegador.close()
    finally:
        proc.terminate()
        log, _ = proc.communicate(timeout=10)
        api.shutdown()
    (saida / "log-programa.txt").write_text(log, encoding="utf-8")
    pedidos = len(api.pedidos)
    print(f"pedidos à API falsa: {pedidos}")
    if erros:
        print("ERROS NA PÁGINA:")
        print("\n".join(erros))
        return 1
    print("página ok, sem erros de JavaScript")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], Path(sys.argv[2])))
