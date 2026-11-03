# 听课助手 启动器
#
# 保存位置规则:
#   - 默认: <工作区>/<课程>/课程转写/  (转写/帧/合成/笔记都在这)
#   - 想固定到某处: 设 TINGKE_DATA / TINGKE_NOTES
#   - 旧数据在别的盘: 设 TINGKE_LEGACY_DATA 让 status 也能看到
#
# 本文件里的两个默认值只服务这台机器; 换机器时按需改或改用环境变量。

$ErrorActionPreference = 'Stop'

# 本机路径配置(不进 git): 存在就加载, 里面放 TINGKE_PYLIBS / TINGKE_PYTHON 等
$localEnv = Join-Path $PSScriptRoot '.env.local.ps1'
if (Test-Path $localEnv) { . $localEnv }

# 依赖目录(约 100 MB, 建议放非系统盘)。优先级:
#   TINGKE_PYLIBS 环境变量 > <本脚本目录>/pylibs
$deps = if ($env:TINGKE_PYLIBS) { $env:TINGKE_PYLIBS } else { Join-Path $PSScriptRoot 'pylibs' }
if (Test-Path $deps) { $env:PYTHONPATH = "$deps;$env:PYTHONPATH" }

# 历史数据目录(可选): 设了 TINGKE_LEGACY_DATA 就让 status/check-note 也扫旧产物
# 本文件不写死任何盘符路径。

# Python 解释器: TINGKE_PYTHON > PATH 里的 python
$py = if ($env:TINGKE_PYTHON) { $env:TINGKE_PYTHON } else { (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $py -or -not (Test-Path $py)) {
    Write-Error "找不到 Python 解释器: 请装 Python 或设 TINGKE_PYTHON"
    exit 2
}

& $py (Join-Path $PSScriptRoot 'scripts\cli.py') @args
exit $LASTEXITCODE
