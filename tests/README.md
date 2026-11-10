# Testes

Os testes usam duas gravações de exemplo do repositório do faster-whisper. Baixe para `tests/_dados/`:

```
mkdir -p tests/_dados
curl -L -o tests/_dados/jfk.flac https://raw.githubusercontent.com/SYSTRAN/faster-whisper/master/tests/data/jfk.flac
curl -L -o tests/_dados/physicsworks.wav https://raw.githubusercontent.com/SYSTRAN/faster-whisper/master/tests/data/physicsworks.wav
```

Depois, na raiz do projeto:

```
uv run --with pytest python -m pytest tests -q
```

- `test_unidades.py`: reamostragem, detecção de fala (comparada com o faster-whisper), limpeza do texto, montagem do pedido ao Claude (cache) e ajustes.
- `test_integracao.py`: o programa inteiro com um transcritor de teste e um servidor falso da API do Claude (`mock_anthropic.py`), usando o SDK de verdade.
- `teste_pagina.py`: abre a página num Chrome sem janela e tira fotos da tela (`python tests/teste_pagina.py <chrome> <pasta>`; precisa do Playwright).

O Whisper de verdade não roda nos testes automáticos (o modelo é baixado da internet na primeira vez que o programa abre).
