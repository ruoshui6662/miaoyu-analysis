param(
  [string]$ApiBase = "http://127.0.0.1:5001",
  [string]$NasBase = "http://192.168.68.112:50560",
  [string]$ApiToken = "",
  [switch]$SkipNas,
  [switch]$SkipOfficial
)

$ErrorActionPreference = "Stop"
$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$libraryRoot = Join-Path $repoRoot "frontend/assets/source-logos/library"
New-Item -ItemType Directory -Force -Path $libraryRoot | Out-Null
$apiHeaders = @{}
$tokenPath = Join-Path $repoRoot "data/admin_token"
if (Test-Path -LiteralPath $tokenPath) {
  $localToken = (Get-Content -LiteralPath $tokenPath -Raw -Encoding UTF8).Trim()
  if ($localToken) { $apiHeaders["X-Admin-Token"] = $localToken }
}
if ($ApiToken) { $apiHeaders["X-Admin-Token"] = $ApiToken }

function Normalize-Text([string]$value) {
  return ([string]$value).ToLowerInvariant() -replace "[\s._\-/:\\()[\].]", ""
}

function Slugify([string]$value, [int]$index) {
  $ascii = ([string]$value).ToLowerInvariant() -replace "[^a-z0-9]+", "-"
  $ascii = $ascii.Trim("-")
  if (-not $ascii) { $ascii = "source-$index" }
  return $ascii
}

function Get-Bytes([string]$url) {
  $client = New-Object System.Net.WebClient
  $client.Headers["User-Agent"] = "Miaoyu-Logo-Library/1.0"
  try { return $client.DownloadData($url) } finally { $client.Dispose() }
}

function Get-AssetKind([byte[]]$bytes) {
  if (-not $bytes -or $bytes.Length -lt 4) { return $null }
  if ($bytes[0] -eq 0x89 -and $bytes[1] -eq 0x50 -and $bytes[2] -eq 0x4e -and $bytes[3] -eq 0x47) { return "png" }
  if ($bytes[0] -eq 0xff -and $bytes[1] -eq 0xd8) { return "jpg" }
  if (($bytes[0] -eq 0x47 -and $bytes[1] -eq 0x49 -and $bytes[2] -eq 0x46)) { return "gif" }
  if (($bytes[0] -eq 0x52 -and $bytes[1] -eq 0x49 -and $bytes[2] -eq 0x46 -and $bytes[8] -eq 0x57 -and $bytes[9] -eq 0x45 -and $bytes[10] -eq 0x42 -and $bytes[11] -eq 0x50)) { return "webp" }
  if (($bytes[0] -eq 0 -and $bytes[1] -eq 0 -and $bytes[2] -eq 1 -and $bytes[3] -eq 0) -or ($bytes[0] -eq 0 -and $bytes[1] -eq 0 -and $bytes[2] -eq 2 -and $bytes[3] -eq 0)) { return "ico" }
  $sampleLength = [Math]::Min(2048, $bytes.Length)
  $sample = [Text.Encoding]::UTF8.GetString($bytes, 0, $sampleLength)
  if ($sample -match "(?is)<svg\b" -and $sample -notmatch "(?is)<html\b|<!doctype") { return "svg" }
  return $null
}

function Save-Asset([byte[]]$bytes, [string]$slug, [string]$sourceUrl) {
  $kind = Get-AssetKind $bytes
  if (-not $kind) { return $null }
  $fileName = "$slug.$kind"
  $filePath = Join-Path $libraryRoot $fileName
  [IO.File]::WriteAllBytes($filePath, $bytes)
  return @{ path = "./$fileName"; kind = $kind; size = $bytes.Length; url = $sourceUrl }
}

function Resolve-IconLinks([string]$siteHost) {
  $base = "https://$siteHost/"
  $result = New-Object System.Collections.Generic.List[string]
  foreach ($path in @("favicon.svg", "favicon.ico", "apple-touch-icon.png", "apple-touch-icon-precomposed.png")) {
    $result.Add($base + $path)
  }
  try {
    $bytes = Get-Bytes $base
    $html = [Text.Encoding]::UTF8.GetString($bytes)
    foreach ($match in [regex]::Matches($html, "(?is)<link\b[^>]*>") ) {
      $tag = $match.Value
      if ($tag -notmatch "(?i)icon") { continue }
      $href = [regex]::Match($tag, '(?is)href\s*=\s*["'']([^"'']+)["'']')
      if (-not $href.Success) { continue }
      try { $result.Add(([uri]::new([uri]$base, $href.Groups[1].Value)).AbsoluteUri) } catch { }
    }
  } catch { }
  return @($result | Select-Object -Unique)
}

function Get-SimpleIconSlug([string]$name, [string[]]$hosts) {
  $n = Normalize-Text $name
  $h = ($hosts -join " ").ToLowerInvariant()
  if ($h -match "weibo\." -or $n -match "weibo") { return "sinaweibo" }
  if ($h -match "zhihu\." -or $n -match "zhihu") { return "zhihu" }
  if ($h -match "bilibili\." -or $n -match "bilibili") { return "bilibili" }
  if ($h -match "xiaohongshu\." -or $n -match "xiaohongshu") { return "xiaohongshu" }
  if ($h -match "baidu\." -or $n -match "baidu") { return "baidu" }
  if ($h -match "weixin\.|qq\." -or $n -match "wechat|weixin") { return "wechat" }
  if ($h -match "douban\." -or $n -match "douban") { return "douban" }
  if ($h -match "v2ex\." -or $n -match "v2ex") { return "v2ex" }
  if ($h -match "douyin\.|tiktok\." -or $n -match "douyin|tiktok") { return "tiktok" }
  return $null
}

function Get-NasCandidate([string]$name, [string[]]$hosts) {
  $terms = @($name) + @($hosts | ForEach-Object { ($_ -split "\.")[0] })
  foreach ($term in ($terms | Where-Object { $_ } | Select-Object -Unique)) {
    try {
      $url = "{0}/images?type=all&search={1}" -f $NasBase, ([uri]::EscapeDataString($term))
      $items = @((Invoke-WebRequest -Uri $url -TimeoutSec 15).Content | ConvertFrom-Json)
      foreach ($item in $items) {
        if (-not $item.name -or $item.name -notmatch "(?i)\.svg$") { continue }
        $stem = Normalize-Text ([IO.Path]::GetFileNameWithoutExtension($item.name))
        $needle = Normalize-Text $term
        $generic = @("www", "news", "app", "home", "site", "com", "cn", "gov", "qq", "163", "sina", "sohu")
        if ($needle.Length -lt 3 -or $generic -contains $needle) { continue }
        $needlePattern = [regex]::Escape($needle)
        if ($stem -notmatch "^$needlePattern(?:-\d+)?$") { continue }
        $assetUrl = "$NasBase/icons/HD-Icons/svg/$($item.name)"
        try { return @{ bytes = Get-Bytes $assetUrl; url = $assetUrl; term = $term } } catch { }
      }
    } catch { }
  }
  return $null
}

try {
  $sourceResponse = (Invoke-WebRequest -Uri "$ApiBase/api/sources" -Headers $apiHeaders -TimeoutSec 15).Content | ConvertFrom-Json
} catch {
  throw "Unable to read source catalog: $ApiBase/api/sources. Start the local service first."
}

$entries = New-Object System.Collections.Generic.List[object]
$seen = @{}
$index = 0
foreach ($group in @($sourceResponse.groups)) {
  foreach ($item in @($group.items)) {
    $index++
    $hosts = @([string]$item.host -split "," | ForEach-Object { $_.Trim().ToLowerInvariant() } | Where-Object { $_ })
    $key = (Normalize-Text $item.name) + "|" + ($hosts -join ",")
    if ($seen.ContainsKey($key)) { continue }
    $seen[$key] = $true
    $slug = Slugify $item.name $index
    $simpleSlug = Get-SimpleIconSlug $item.name $hosts
    $asset = $null
    $provider = $null
    $sourceUrl = $null
    $notes = New-Object System.Collections.Generic.List[string]

    if ($simpleSlug) {
      $simpleUrl = "https://cdn.simpleicons.org/$simpleSlug"
      try {
        $asset = Save-Asset (Get-Bytes $simpleUrl) $slug $simpleUrl
        if ($asset) { $provider = "simple-icons"; $sourceUrl = $simpleUrl; $notes.Add("Verified local brand mapping") }
      } catch { }
    }

    if (-not $asset -and -not $SkipOfficial -and $hosts.Count) {
      $officialCandidates = New-Object System.Collections.Generic.List[string]
      if ($hosts -contains "mps.gov.cn") { $officialCandidates.Add("https://ywtb.mps.gov.cn/newhome/favicon.ico") }
      foreach ($iconUrl in (Resolve-IconLinks $hosts[0])) { $officialCandidates.Add($iconUrl) }
      foreach ($iconUrl in ($officialCandidates | Select-Object -Unique)) {
        try {
          $asset = Save-Asset (Get-Bytes $iconUrl) $slug $iconUrl
          if ($asset) { $provider = "official-site"; $sourceUrl = $iconUrl; $notes.Add("Official site icon; review brand rules before release"); break }
        } catch { }
      }
    }

    if (-not $asset -and -not $SkipNas -and $group.key -ne "gov") {
      $candidate = Get-NasCandidate $item.name $hosts
      if ($candidate) {
        $asset = Save-Asset $candidate.bytes $slug $candidate.url
        if ($asset) { $provider = "nas-hd-icons"; $sourceUrl = $candidate.url; $notes.Add("High-confidence NAS SVG candidate; review before release") }
      }
    }

    $status = if ($asset) { "fetched" } else { "pending" }
    if (-not $hosts.Count) { $notes.Add("No website host; keep as pending catalog item") }
    $providerValue = if ($provider) { $provider } else { "none" }
    $assetValue = if ($asset) { $asset.path } else { "" }
    $sourceUrlValue = if ($sourceUrl) { $sourceUrl } else { "" }
    $assetKindValue = if ($asset) { $asset.kind } else { "" }
    $assetSizeValue = if ($asset) { $asset.size } else { 0 }
    $entry = [ordered]@{
      name = $item.name
      aliases = @($item.name)
      hosts = $hosts
      category = $group.key
      slug = $slug
      status = $status
      provider = $providerValue
      asset = $assetValue
      source_url = $sourceUrlValue
      asset_kind = $assetKindValue
      asset_size = $assetSizeValue
      license = if ($provider -eq "simple-icons") { "CC0-1.0 (per asset library; trademarks remain with owners)" } elseif ($provider -eq "official-site") { "Official site icon; review site brand/use rules" } elseif ($provider -eq "nas-hd-icons") { "NAS asset license pending" } else { "Not fetched" }
      notes = @($notes)
      checked_at = (Get-Date).ToUniversalTime().ToString("o")
    }
    $entries.Add([pscustomobject]$entry)
    $mark = if ($asset) { "$provider/$($asset.kind)" } else { "pending" }
    Write-Host ("[{0}/{1}] {2} -> {3}" -f $index, $sourceResponse.total, $item.name, $mark)
  }
}

$entryArray = [object[]]@($entries | ForEach-Object { $_ })
$manifest = [ordered]@{
  schema_version = 1
  generated_at = (Get-Date).ToUniversalTime().ToString("o")
  nas_base = $NasBase
  policy = "Only content-signature-validated image/SVG files are stored; similar candidates never masquerade as official logos"
  entries = $entryArray
}
$manifest | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $libraryRoot "manifest.json") -Encoding UTF8
Write-Host "Logo library generated: $libraryRoot"
Write-Host "Fetched: $(($entries | Where-Object status -eq 'fetched').Count); pending: $(($entries | Where-Object status -eq 'pending').Count)"
