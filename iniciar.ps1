#Requires -Version 5.1
<#
  Iniciador do Assistente ao Vivo. O atalho da Área de Trabalho roda este arquivo.
   1. Baixa o uv (instala o Python e as bibliotecas), só na primeira vez.
   2. Baixa do GitHub a versão mais nova do programa, se houver.
   3. Abre o programa (a página abre sozinha no navegador).
  Este arquivo é gravado em UTF-8 com BOM, para os acentos saírem certos no PowerShell 5.1.
#>
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$Repo = 'jpvanacor/assistente-ao-vivo'
$Base = Join-Path $env:LOCALAPPDATA 'AssistenteAoVivo'
$App = Join-Path $Base 'app'
try { $Host.UI.RawUI.WindowTitle = 'Assistente ao Vivo (feche esta janela para encerrar o programa)' } catch {}

function Passo([string]$Texto) { Write-Host ''; Write-Host ('==> ' + $Texto) -ForegroundColor Cyan }
function Aviso([string]$Texto) { Write-Host ('    ' + $Texto) -ForegroundColor Yellow }
function Sair([string]$Texto) {
    Write-Host ''
    Write-Host $Texto -ForegroundColor Red
    try {
        Add-Type -AssemblyName PresentationFramework
        [void][System.Windows.MessageBox]::Show($Texto, 'Assistente ao Vivo')
    } catch {}
    Read-Host 'Pressione Enter para fechar'
    exit 1
}

New-Item -ItemType Directory -Force -Path $Base | Out-Null

# 1. uv ---------------------------------------------------------------------------
$Uv = Join-Path $Base 'uv\uv.exe'
if (-not (Test-Path -LiteralPath $Uv)) {
    Passo 'Baixando o uv (instala o Python e as bibliotecas). Só na primeira vez.'
    $ZipUv = Join-Path $env:TEMP 'uv-assistente-ao-vivo.zip'
    try {
        Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip' -OutFile $ZipUv
        Expand-Archive -LiteralPath $ZipUv -DestinationPath (Join-Path $Base 'uv') -Force
    } catch {
        Sair ('Não consegui baixar o uv: ' + $_)
    } finally {
        Remove-Item -LiteralPath $ZipUv -Force -ErrorAction SilentlyContinue
    }
    if (-not (Test-Path -LiteralPath $Uv)) { Sair ('Baixei o uv, mas não achei ' + $Uv) }
}

# 2. Atualização ------------------------------------------------------------------
Passo 'Procurando versão nova'
$ArquivoVersao = Join-Path $Base 'versao.txt'
$VersaoLocal = ''
if (Test-Path -LiteralPath $ArquivoVersao) { $VersaoLocal = (Get-Content -LiteralPath $ArquivoVersao -Raw).Trim() }
$TemApp = Test-Path -LiteralPath (Join-Path $App 'assistente\__main__.py')
$Sha = ''
try {
    $Resposta = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/commits/main" -UserAgent 'AssistenteAoVivo' -TimeoutSec 20
    $Sha = [string]$Resposta.sha
} catch {
    Aviso 'Não consegui consultar o GitHub agora.'
}

if ($Sha -and (($Sha -ne $VersaoLocal) -or (-not $TemApp))) {
    Write-Host ('    Baixando a versão ' + $Sha.Substring(0, 7))
    $Zip = Join-Path $env:TEMP ('assistente-ao-vivo-' + $Sha.Substring(0, 7) + '.zip')
    $Tmp = Join-Path $env:TEMP ('assistente-ao-vivo-' + [guid]::NewGuid().ToString('N'))
    try {
        Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/$Repo/archive/$Sha.zip" -OutFile $Zip
        Expand-Archive -LiteralPath $Zip -DestinationPath $Tmp -Force
        $Raiz = Get-ChildItem -LiteralPath $Tmp -Directory | Select-Object -First 1
        if (-not $Raiz -or -not (Test-Path -LiteralPath (Join-Path $Raiz.FullName 'assistente\__main__.py'))) {
            throw 'o pacote baixado veio incompleto'
        }
        if (Test-Path -LiteralPath $App) { Remove-Item -LiteralPath $App -Recurse -Force }
        Move-Item -LiteralPath $Raiz.FullName -Destination $App
        Set-Content -LiteralPath $ArquivoVersao -Value $Sha -Encoding Ascii
        Copy-Item -LiteralPath (Join-Path $App 'iniciar.ps1') -Destination (Join-Path $Base 'iniciar.ps1') -Force
        $TemApp = $true
        Write-Host '    Atualizado.'
    } catch {
        if (-not (Test-Path -LiteralPath (Join-Path $App 'assistente\__main__.py'))) {
            Sair ('Não consegui baixar o programa: ' + $_)
        }
        Aviso ('Não consegui atualizar (' + $_ + '). Abrindo a versão que já está aqui.')
    } finally {
        Remove-Item -LiteralPath $Zip -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $Tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
} elseif (-not $TemApp) {
    Sair 'Não consegui baixar o programa. Confira a internet e abra de novo.'
} else {
    Write-Host '    Já está na versão mais nova.'
}

# 3. Abrir --------------------------------------------------------------------------
Passo 'Abrindo o Assistente ao Vivo'
Write-Host '    Na primeira vez o uv baixa o Python e as bibliotecas (cerca de 120 MB). Pode levar alguns minutos.'
Write-Host '    Para encerrar o programa, feche esta janela.'
$env:UV_PROJECT_ENVIRONMENT = Join-Path $Base 'venv'
$env:HF_HUB_DISABLE_SYMLINKS_WARNING = '1'
$env:PYTHONUTF8 = '1'
Set-Location -LiteralPath $App
& $Uv run --frozen --python 3.12 python -m assistente
$Codigo = $LASTEXITCODE
if ($Codigo -ne 0) {
    Sair ('O programa fechou com erro (código ' + $Codigo + '). Copie as mensagens acima e mande para o Claude.')
}
