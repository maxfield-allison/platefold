# Build a private Windows review candidate; no signing or installation on this host.
[CmdletBinding()]
param([string]$OutputDirectory = 'dist/windows-candidate')
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program failed with exit code $LASTEXITCODE" }
}

$root = Split-Path $PSScriptRoot -Parent
Push-Location $root
try {
    if ($env:OS -ne 'Windows_NT' -or -not [Environment]::Is64BitProcess) {
        throw 'Build with native 64-bit PowerShell on Windows.'
    }
    $output = [IO.Path]::GetFullPath((Join-Path $root $OutputDirectory))
    if (Test-Path -LiteralPath $output) { throw 'Output already exists; preserve it and choose a new directory.' }
    foreach ($command in @('py.exe', 'node.exe', 'npm.cmd', 'rustup.exe', 'git.exe')) {
        if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
            throw "Missing build prerequisite: $command"
        }
    }
    if ((& node.exe --version) -cne 'v22.23.2') { throw 'Node.js 22.23.2 is required.' }
    Invoke-Checked 'py.exe' @('-3.11', '-c', 'import sys; assert sys.maxsize > 2**32')
    $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
    if (-not (Test-Path -LiteralPath $vswhere)) { throw 'Visual Studio Build Tools are required.' }
    $installation = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($installation)) { throw 'MSVC x64 tools are missing.' }
    $devCmd = Join-Path $installation 'Common7\Tools\VsDevCmd.bat'
    $developerEnvironment = & cmd.exe /d /s /c "`"$devCmd`" -no_logo -arch=x64 -host_arch=x64 && set"
    if ($LASTEXITCODE -ne 0) { throw 'Could not initialize MSVC.' }
    foreach ($line in $developerEnvironment) {
        if ($line -match '^([^=]+)=(.*)$') {
            [Environment]::SetEnvironmentVariable($matches[1], $matches[2], 'Process')
        }
    }
    if (-not (Get-Command link.exe -ErrorAction SilentlyContinue)) { throw 'MSVC linker is unavailable.' }
    $env:RUSTUP_HOME = Join-Path $root ('build/windows-candidate-rustup-' +
        [guid]::NewGuid().ToString('N'))
    $rustReceipt = Join-Path $root ('build/rust-toolchain-' + [guid]::NewGuid().ToString('N') + '.json')
    Invoke-Checked 'py.exe' @('-3.11', 'tools/rust_toolchain.py', 'install', '--receipt', $rustReceipt)
    $venv = Join-Path $root 'build/windows-candidate-venv'
    Invoke-Checked 'py.exe' @('-3.11', '-m', 'venv', $venv)
    $python = Join-Path $venv 'Scripts/python.exe'
    # Hash-pinned wheels only, then the project itself with nothing downloaded.
    Invoke-Checked $python @('-m', 'pip', 'install', '--require-hashes', '--only-binary=:all:',
        '-r', 'packaging/native-requirements/windows-x86_64.txt')
    Invoke-Checked $python @('-m', 'pip', 'install', '--no-deps', '--no-build-isolation',
        '--no-index', '-e', '.')
    # A new browser directory, filled only from archives with the reviewed digests.
    $env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $root ('build/windows-candidate-browsers-' +
        [guid]::NewGuid().ToString('N'))
    $downloads = Join-Path $root ('build/native-downloads-' + [guid]::NewGuid().ToString('N') + '.json')
    Invoke-Checked $python @('tools/native_downloads.py', 'prepare', '--receipt', $downloads,
        '--rust-receipt', $rustReceipt)
    Invoke-Checked $python @('tools/build_desktop.py', '--output-dir', $output,
        '--verified-downloads', $downloads)
    $receipt = Join-Path $output 'BUILD-RECEIPT.json'
    $revision = (& git.exe rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Could not identify source revision.' }
    $inspection = Join-Path $root ('build/windows-candidate-check-' + [guid]::NewGuid().ToString('N'))
    Invoke-Checked $python @('tools/check_windows_package.py', $receipt, $inspection,
        '--expected-commit', $revision)
    Copy-Item -LiteralPath (Join-Path $inspection 'PACKAGE-CHECK.json') -Destination $output
    Write-Host 'Private Windows package built and checked. Signing, installation and owner review remain separate.'
} finally {
    Pop-Location
}
