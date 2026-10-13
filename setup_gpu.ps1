$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
& $python -m pip install --upgrade --no-deps --target work/cuda-runtime 'https://download.pytorch.org/whl/cu128/torch-2.11.0%2Bcu128-cp312-cp312-win_amd64.whl#sha256=7c78215c3af4f62e63f2b2e360f1722fc719b0853c7ac22666483d9810613a4c'
if ($LASTEXITCODE -ne 0) { throw 'CUDA PyTorch installation failed' }
& $python -m pip install --upgrade --no-deps --target work/cuda-runtime torchaudio==2.11.0 torchvision==0.26.0 --index-url https://download.pytorch.org/whl/cu128
if ($LASTEXITCODE -ne 0) { throw 'CUDA audio/vision installation failed' }
Write-Host 'CUDA runtime ready. Set ASR_DEVICE=cuda:0 in .env before starting a new course task.'
