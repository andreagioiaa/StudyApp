# StudyFlow - build automatica dell'eseguibile one-file.
# Uso:  .\build.ps1
#       .\build.ps1 -SkipTests

param(
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

try {
    $venvPath = Join-Path $PSScriptRoot ".venv"
    $venvPython = Join-Path $venvPath "Scripts\python.exe"

    Write-Host "== StudyFlow build ==" -ForegroundColor Cyan

    if (-not (Test-Path $venvPython)) {
        Write-Host "Creo il virtual environment con Python 3.12..." -ForegroundColor Yellow
        $py = Get-Command py -ErrorAction SilentlyContinue
        if ($py) {
            & py -3.12 -m venv $venvPath
        }
        else {
            $python = Get-Command python -ErrorAction SilentlyContinue
            if (-not $python) {
                throw "Python non trovato. Installa Python 3.12 a 64 bit e riprova."
            }
            & python -m venv $venvPath
        }
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPython)) {
            throw "Creazione del virtual environment non riuscita."
        }
    }

    & $venvPython -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) {
        throw "Aggiornamento di pip non riuscito."
    }

    & $venvPython -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) {
        throw "Installazione delle dipendenze non riuscita."
    }

    if (-not $SkipTests) {
        Write-Host "Eseguo i test..." -ForegroundColor Yellow
        & $venvPython -m unittest discover -s tests -v
        if ($LASTEXITCODE -ne 0) {
            throw "Test falliti: build interrotta."
        }
    }

    if (-not (Test-Path "assets\app.ico")) {
        throw "Icona assets\app.ico mancante."
    }

    Write-Host "Compilazione con PyInstaller..." -ForegroundColor Yellow
    & $venvPython -m PyInstaller --noconfirm --clean StudyFlow.spec
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller ha restituito un errore."
    }

    $exe = Join-Path $PSScriptRoot "dist\StudyFlow.exe"
    if (-not (Test-Path $exe)) {
        throw "Build terminata ma dist\StudyFlow.exe non esiste."
    }

    # Pandoc per l'esportazione Word: copiato accanto all'exe (non dentro, per non
    # estrarre ~150 MB a ogni avvio). StudyFlow lo cerca in quella cartella.
    $ErrorActionPreference = "Continue"
    $pandocSrc = & $venvPython -c "import pypandoc; print(pypandoc.get_pandoc_path())" 2>$null
    $pandocExit = $LASTEXITCODE
    $ErrorActionPreference = "Stop"

    if ($pandocExit -eq 0 -and $pandocSrc -and (Test-Path $pandocSrc)) {
        Copy-Item $pandocSrc (Join-Path $PSScriptRoot "dist\pandoc.exe") -Force
        Write-Host "Pandoc copiato in dist\pandoc.exe" -ForegroundColor DarkGray
    }
    else {
        Write-Host "Pandoc non trovato: l'esportazione .docx richiedera' pandoc nel PATH." -ForegroundColor Yellow
    }

    $sizeMb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
    Write-Host ""
    Write-Host "Build completata: $exe ($sizeMb MB)" -ForegroundColor Green
    Write-Host "Avvialo normalmente: l'app resta asInvoker e chiedera' UAC solo al primo blocco siti." -ForegroundColor DarkGray
}
catch {
    Write-Host ""
    Write-Host "=== BUILD FALLITA ===" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host $_.InvocationInfo.PositionMessage -ForegroundColor DarkRed
}
finally {
    Write-Host ""
    Read-Host "Premi INVIO per chiudere la finestra"
}