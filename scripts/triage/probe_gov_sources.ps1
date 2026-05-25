# ============================================================================
# scripts/triage/probe_gov_sources.ps1
# Triage probe for the 6 government sources that fail in MÜŞAHİT runs.
# Read-only · safe to re-run · no DB writes · no Python deps · just HTTP.
# ============================================================================

$ErrorActionPreference = "Continue"

# The six gov sources from sources.py
$sources = @(
    @{ id = "resmi_gazete";      url = "https://www.resmigazete.gov.tr/"; kind = "PDF"  },
    @{ id = "cumhurbaskanligi";  url = "https://www.tccb.gov.tr/";        kind = "HTML" },
    @{ id = "anayasa_mahkemesi"; url = "https://www.anayasa.gov.tr/";     kind = "HTML" },
    @{ id = "yargitay";          url = "https://www.yargitay.gov.tr/";    kind = "HTML" },
    @{ id = "danistay";          url = "https://www.danistay.gov.tr/";    kind = "HTML" },
    @{ id = "kap";               url = "https://www.kap.org.tr/";         kind = "RSS"  }
)

# Three User-Agent variants to probe
$uaVariants = @(
    @{ name = "PYTHON-REQUESTS";  ua = "python-requests/2.31.0"                                                           },
    @{ name = "CHROME-DESKTOP";   ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36" },
    @{ name = "MUSAHIT-DEFAULT";  ua = "MUSAHIT/0.1 (personal OSINT)"                                                     }
)

# Output directory · keep results for comparison
$outDir = "logs/gov-source-triage"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$reportPath = "$outDir/probe-$timestamp.txt"

# Helper · probe one URL with one UA
function Probe-Url {
    param(
        [string]$Url,
        [string]$UserAgent,
        [int]$TimeoutSec = 15
    )
    $result = [ordered]@{
        status_code   = $null
        content_type  = ""
        body_length   = 0
        body_preview  = ""
        elapsed_ms    = 0
        error         = ""
    }
    $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $response = Invoke-WebRequest `
            -Uri $Url `
            -UserAgent $UserAgent `
            -TimeoutSec $TimeoutSec `
            -MaximumRedirection 5 `
            -ErrorAction Stop `
            -UseBasicParsing
        $stopwatch.Stop()
        $result.status_code  = [int]$response.StatusCode
        $result.content_type = $response.Headers["Content-Type"]
        $result.body_length  = $response.RawContentLength
        if ($response.Content) {
            $preview = $response.Content.Substring(0, [Math]::Min(200, $response.Content.Length))
            $result.body_preview = $preview -replace "[\r\n\t]+", " "
        }
    } catch {
        $stopwatch.Stop()
        $result.error = $_.Exception.Message
        if ($_.Exception.Response) {
            $result.status_code = [int]$_.Exception.Response.StatusCode
            $result.content_type = $_.Exception.Response.Headers["Content-Type"]
        }
    }
    $result.elapsed_ms = [int]$stopwatch.ElapsedMilliseconds
    return $result
}

# Header
$header = "❯ MÜŞAHİT GOV SOURCE TRIAGE · $timestamp"
$divider = "─" * 78
$report = @($header, $divider, "")

Write-Host $header -ForegroundColor Cyan
Write-Host $divider -ForegroundColor DarkGray
Write-Host ""

foreach ($source in $sources) {
    $sectionHeader = "❯ $($source.id)  (kind=$($source.kind))  $($source.url)"
    $report += $sectionHeader
    $report += ("·" * 78)
    Write-Host $sectionHeader -ForegroundColor Yellow

    $statusCodes = @()

    foreach ($variant in $uaVariants) {
        Write-Host "  probing with $($variant.name)..." -ForegroundColor DarkGray
        $r = Probe-Url -Url $source.url -UserAgent $variant.ua
        $statusCodes += $r.status_code

        $report += "  [$($variant.name)]"
        $report += "    status         · $($r.status_code)"
        $report += "    content_type   · $($r.content_type)"
        $report += "    body_length    · $($r.body_length) bytes"
        $report += "    elapsed        · $($r.elapsed_ms) ms"
        if ($r.error) {
            $report += "    error          · $($r.error)"
        }
        if ($r.body_preview) {
            $report += "    body_preview   · $($r.body_preview)"
        }
        $report += ""

        # be polite · don't hammer the same source
        Start-Sleep -Milliseconds 800
    }

    # Quick verdict line
    $uniqueCodes = $statusCodes | Sort-Object -Unique
    $verdict = if ($uniqueCodes.Count -eq 1 -and $uniqueCodes[0] -eq 200) {
        "ALL UAs OK · URL serves content"
    } elseif ($uniqueCodes -contains 200) {
        "UA-dependent · some succeed, some fail"
    } elseif ($uniqueCodes -contains 403) {
        "BLOCKED · 403 across UAs (probably WAF / Cloudflare)"
    } elseif ($uniqueCodes -contains 404) {
        "URL ROT · 404 · path probably moved"
    } elseif ($uniqueCodes -contains 503) {
        "RATE LIMITED or down · 503"
    } elseif ($uniqueCodes -contains $null) {
        "TIMEOUT or DNS failure across UAs"
    } else {
        "MIXED · codes: $($uniqueCodes -join ', ')"
    }
    $report += "  ❯ VERDICT · $verdict"
    $report += ""
    Write-Host "  → $verdict" -ForegroundColor Green
    Write-Host ""

    # pause between sources too
    Start-Sleep -Seconds 1
}

$report += $divider
$report += "❯ Report saved to: $reportPath"

# Write report
$report | Out-File -FilePath $reportPath -Encoding UTF8
Write-Host "Report saved: $reportPath" -ForegroundColor Cyan
