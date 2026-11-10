# Instalador do Assistente ao Vivo (roda uma vez).
# Uso: abra o Windows PowerShell, cole a linha abaixo e aperte Enter:
#   irm https://raw.githubusercontent.com/jpvanacor/assistente-ao-vivo/main/instalar.ps1 | iex
#
# O que ele faz:
#   1. Grava o iniciador em %LOCALAPPDATA%\AssistenteAoVivo\iniciar.ps1
#   2. Cria o atalho "Assistente ao Vivo" na Area de Trabalho
#   3. Abre o programa pela primeira vez (o iniciador baixa o resto sozinho)
# (Este arquivo fica sem acentos de proposito: o Windows PowerShell 5.1 le arquivos
#  sem BOM como ANSI.)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$Repo = 'jpvanacor/assistente-ao-vivo'
$Base = Join-Path $env:LOCALAPPDATA 'AssistenteAoVivo'
$PowerShellExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
New-Item -ItemType Directory -Force -Path $Base | Out-Null

Write-Host ''
Write-Host '==> 1/3 Baixando o iniciador' -ForegroundColor Cyan
$Iniciador = Join-Path $Base 'iniciar.ps1'
Invoke-WebRequest -UseBasicParsing -Uri "https://raw.githubusercontent.com/$Repo/main/iniciar.ps1" -OutFile $Iniciador

Write-Host '==> 2/3 Criando o atalho na Area de Trabalho' -ForegroundColor Cyan
$Desktop = [Environment]::GetFolderPath('Desktop')
$Shell = New-Object -ComObject WScript.Shell
$Atalho = $Shell.CreateShortcut((Join-Path $Desktop 'Assistente ao Vivo.lnk'))
$Atalho.TargetPath = $PowerShellExe
$Atalho.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $Iniciador + '"'
$Atalho.WorkingDirectory = $Base
$Atalho.IconLocation = (Join-Path $env:SystemRoot 'System32\SndVol.exe') + ',0'
$Atalho.WindowStyle = 7
$Atalho.Description = 'Assistente ao Vivo'
$Atalho.Save()
Write-Host ('    Atalho: ' + (Join-Path $Desktop 'Assistente ao Vivo.lnk'))

Write-Host '==> 3/3 Abrindo pela primeira vez (baixa o Python, as bibliotecas e o Whisper)' -ForegroundColor Cyan
& $PowerShellExe -NoProfile -ExecutionPolicy Bypass -File $Iniciador
