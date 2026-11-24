param([string]$Key = $env:BAMPIAO_KEY)
$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($Key)) {
  throw 'Missing Bampiao key. Pass it as an argument or set BAMPIAO_KEY.'
}

$url = 'https://hezhuzi.com/bp/skill/bampiao-skill.zip'
$tmp = Join-Path ([IO.Path]::GetTempPath()) ('bampiao-skill-' + [Guid]::NewGuid().ToString('N'))
$zip = Join-Path $tmp 'bampiao-skill.zip'
$src = Join-Path $tmp 'src'
New-Item -ItemType Directory -Force -Path $src | Out-Null
try {
  Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $zip
  Expand-Archive -LiteralPath $zip -DestinationPath $src -Force
  $stamp = Get-Date -Format 'yyyyMMddHHmmss'
  $bases = @(
    (Join-Path $HOME '.workbuddy'), (Join-Path $HOME '.hermes'),
    (Join-Path $HOME '.claude'), (Join-Path $HOME '.codex'),
    (Join-Path $HOME '.cursor'), (Join-Path $HOME '.openclaw'),
    (Join-Path $HOME '.windsurf'), (Join-Path $HOME '.trae'),
    (Join-Path $HOME '.gemini/antigravity'), (Join-Path $HOME '.agents'),
    (Join-Path $HOME '.dsh')
  )
  foreach ($base in $bases) {
    if (-not (Test-Path -LiteralPath $base -PathType Container)) { continue }
    $skills = Join-Path $base 'skills'
    $dest = Join-Path $skills 'bampiao'
    New-Item -ItemType Directory -Force -Path $skills | Out-Null
    if (Test-Path -LiteralPath $dest) { Move-Item -LiteralPath $dest -Destination "$dest.previous-$stamp" }
    Copy-Item -LiteralPath $src -Destination $dest -Recurse
  }
  $homeSkill = Join-Path $HOME '.bampiao/skill'
  New-Item -ItemType Directory -Force -Path (Split-Path $homeSkill) | Out-Null
  if (Test-Path -LiteralPath $homeSkill) { Move-Item -LiteralPath $homeSkill -Destination "$homeSkill.previous-$stamp" }
  Copy-Item -LiteralPath $src -Destination $homeSkill -Recurse
  $keyFile = Join-Path $HOME '.bampiao/key'
  [IO.File]::WriteAllText($keyFile, $Key.Trim() + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
  & icacls $keyFile /inheritance:r /grant:r "$env:USERNAME:(R,W)" | Out-Null
  $py = Get-Command py -ErrorAction SilentlyContinue
  if ($py) {
    & $py.Source -3 (Join-Path $homeSkill 'scripts/bampiao_draw.py') --check
  } else {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
      & $python.Source (Join-Path $homeSkill 'scripts/bampiao_draw.py') --check
    } else {
      $node = Get-Command node -ErrorAction SilentlyContinue
      if (-not $node) { throw 'Skill installed, but Python 3 or Node 18+ is required for self-check.' }
      & $node.Source (Join-Path $homeSkill 'scripts/bampiao_draw.mjs') --check
    }
  }
  Write-Output 'Installed. In any new Agent window, say: use Bampiao to generate an image.'
} finally {
  if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Recurse -Force }
}
