#Requires -Version 5.1
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [ValidateNotNullOrEmpty()]
  [string]$RequestId,

  [switch]$Full
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$LogsDir = Join-Path $RepoRoot "logs"

function Read-JsonObjects {
  param([string]$Path)
  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return }
  $buffer = New-Object System.Collections.Generic.List[string]
  $depth = 0
  foreach ($line in (Get-Content -LiteralPath $Path -ErrorAction SilentlyContinue)) {
    $trimmed = $line.Trim()
    if (-not $trimmed) { continue }
    if ($depth -eq 0 -and $trimmed.StartsWith("{") -and $trimmed.EndsWith("}")) {
      try { $obj = $trimmed | ConvertFrom-Json; if ($obj) { $obj } } catch { }
      continue
    }
    if ($depth -eq 0 -and $trimmed -ne "{") { continue }
    $buffer.Add($line)
    $safe = [regex]::Replace(($line -replace '\\\\.', ''), '"[^"]*"', '""')
    $depth += ([regex]::Matches($safe, "{")).Count
    $depth -= ([regex]::Matches($safe, "}")).Count
    if ($depth -le 0) {
      try { $obj = ($buffer -join "`n") | ConvertFrom-Json; if ($obj) { $obj } } catch { }
      $buffer.Clear()
      $depth = 0
    }
  }
}

function ValueOf {
  param([object]$Object, [string[]]$Names)
  if ($null -eq $Object) { return "" }
  foreach ($name in $Names) {
    $property = $Object.PSObject.Properties[$name]
    if ($property -and $null -ne $property.Value -and "$($property.Value)" -ne "") {
      return $property.Value
    }
  }
  return ""
}

function TextOf {
  param([object]$Value)
  if ($null -eq $Value) { return "" }
  if ($Value -is [pscustomobject] -or $Value -is [System.Collections.IDictionary]) {
    return ($Value | ConvertTo-Json -Compress -Depth 8)
  }
  return "$Value"
}

function Add-Log {
  param([object]$Object, [string]$Source)
  [pscustomobject]@{
    Timestamp = ValueOf $Object @("timestamp", "time")
    Level = ValueOf $Object @("level", "severity")
    Event = ValueOf $Object @("event", "operation", "message")
    Operation = ValueOf $Object @("operation")
    Method = ValueOf $Object @("method")
    Path = ValueOf $Object @("path", "route")
    HttpStatus = ValueOf $Object @("status_code", "status")
    RequestId = ValueOf $Object @("request_id", "requestId")
    TraceId = ValueOf $Object @("trace_id", "traceId")
    SpanId = ValueOf $Object @("span_id", "spanId")
    Service = ValueOf $Object @("service", "service.name", "logger", "component")
    Component = ValueOf $Object @("component", "service", "logger")
    RawResult = ValueOf $Object @("result")
    DurationMs = ValueOf $Object @("duration_ms", "durationMs")
    ErrorType = ValueOf $Object @("error_type", "errorType")
    Error = ValueOf $Object @("error", "exception", "message")
    Source = $Source
  }
}

function Shorten {
  param(
    [object]$Value,
    [int]$MaxLength = 220
  )
  $text = (TextOf $Value) -replace "\s+", " "
  if ($text.Length -le $MaxLength) { return $text }
  return $text.Substring(0, $MaxLength - 3) + "..."
}

function Is-FailureLog {
  param([object]$Item)
  if ($Item.ErrorType) { return $true }
  if ($Item.Level -eq "error") { return $true }
  return $Item.RawResult -in @("error", "timeout", "cancelled", "failed")
}

function Get-ComponentName {
  param([object]$Item)
  if ($Item.Component) { return "$($Item.Component)" }
  if ($Item.Service) { return "$($Item.Service)" }
  return "unknown"
}

$logFiles = @(
  Get-ChildItem -LiteralPath $LogsDir -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -like "runtime-diagnostics.jsonl*" -or $_.Name -like "server.out.log*" } |
    Sort-Object LastWriteTime
)
$traceFiles = @(
  Get-ChildItem -LiteralPath $LogsDir -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -like "traces.jsonl*" } |
    Sort-Object LastWriteTime
)

$logs = New-Object System.Collections.Generic.List[object]
$traceCandidates = New-Object System.Collections.Generic.List[object]
$traceIds = New-Object System.Collections.Generic.HashSet[string]

foreach ($file in $logFiles) {
  foreach ($object in (Read-JsonObjects $file.FullName)) {
    $record = if ($object.kind -and $object.record) { $object.record } else { $object }
    $request = ValueOf $record @("request_id", "requestId")
    $isTrace = $object.name -and $object.context
    if ($request -eq $RequestId -and -not $isTrace) {
      $item = Add-Log $record "$($file.Name)"
      $logs.Add($item)
      if ($item.TraceId) { [void]$traceIds.Add(("$($item.TraceId)").TrimStart("0x")) }
    }
    if ($isTrace) {
      $traceCandidates.Add([pscustomobject]@{ Object = $object; Source = $file.Name })
    }
  }
}
foreach ($file in $traceFiles) {
  foreach ($object in (Read-JsonObjects $file.FullName)) {
    $traceCandidates.Add([pscustomobject]@{ Object = $object; Source = $file.Name })
  }
}

$traces = New-Object System.Collections.Generic.List[object]
foreach ($candidate in $traceCandidates) {
  $object = $candidate.Object
  $attributes = $object.attributes
  $request = ValueOf $attributes @("request_id", "requestId")
  $context = $object.context
  $traceId = ValueOf $context @("trace_id", "traceId")
  $normalized = ("$traceId").TrimStart("0x")
  if ($request -eq $RequestId -or ($normalized -and $traceIds.Contains($normalized))) {
    $traceComponent = ValueOf $attributes @("component")
    if (-not $traceComponent) { $traceComponent = ($object.name -split "\.")[0] }
    $traces.Add([pscustomobject]@{
      Timestamp = ValueOf $object @("start_time", "timestamp")
      Name = ValueOf $object @("name", "operation")
      TraceId = $traceId
      SpanId = ValueOf $context @("span_id", "spanId")
      Status = TextOf $object.status
      Attributes = TextOf $attributes
      Component = $traceComponent
      DurationMs = ValueOf $attributes @("duration_ms", "durationMs")
      Error = ValueOf $object.status @("description")
      Source = $candidate.Source
    })
  }
}

$logs = @($logs | Sort-Object { try { [datetime]$_.Timestamp } catch { [datetime]::MinValue } })
$traces = @($traces | Sort-Object { try { [datetime]$_.Timestamp } catch { [datetime]::MinValue } })

if ($logs.Count -eq 0 -and $traces.Count -eq 0) {
  Write-Host "REQUEST SUMMARY" -ForegroundColor Cyan
  Write-Host ("Request ID: {0}" -f $RequestId)
  Write-Host "No observations found for this request_id"
  Write-Host "Metrics: use /metrics for aggregate service status"
  exit 2
}

$failureLogs = @($logs | Where-Object { Is-FailureLog $_ })
$failureTraces = @($traces | Where-Object { $_.Error -or $_.Status -match '"ERROR"' })
$failed = $failureLogs.Count -gt 0 -or $failureTraces.Count -gt 0
$statusText = if ($failed) { "FAILED" } else { "SUCCESS" }

$httpRecord = @(
  $logs |
    Where-Object { $_.Method -and $_.Path } |
    Sort-Object { try { [double]$_.DurationMs } catch { 0 } } -Descending |
    Select-Object -First 1
)[0]
$methodPath = if ($httpRecord) { "$($httpRecord.Method) $($httpRecord.Path)" } else { "not recorded" }
$httpStatus = if ($httpRecord -and $httpRecord.HttpStatus) { $httpRecord.HttpStatus } else { "not recorded" }

$durationRecord = @(
  $logs |
    Where-Object { $_.DurationMs } |
    Sort-Object { try { [double]$_.DurationMs } catch { 0 } } -Descending |
    Select-Object -First 1
)[0]
$totalDuration = if ($durationRecord) { "$($durationRecord.DurationMs) ms" } else { "not recorded" }

$traceId = @($logs | Where-Object { $_.TraceId } | Select-Object -First 1).TraceId
if (-not $traceId) { $traceId = @($traces | Where-Object { $_.TraceId } | Select-Object -First 1).TraceId }

$rootFailure = @($failureTraces | Where-Object { $_.Error } | Select-Object -First 1)[0]
if (-not $rootFailure) { $rootFailure = @($failureLogs | Where-Object { $_.Error } | Select-Object -First 1)[0] }
$keyError = if ($rootFailure) { Shorten $rootFailure.Error } else { "none" }
$errorType = if ($failureLogs.Count -gt 0) {
  @($failureLogs | Where-Object { $_.ErrorType } | Select-Object -First 1).ErrorType
} elseif ($failureTraces.Count -gt 0) {
  "trace_error"
} else {
  ""
}
$failureComponent = if ($rootFailure) {
  if ($rootFailure.Component) { $rootFailure.Component } else { Get-ComponentName $rootFailure }
} else {
  "none"
}

$downstream = New-Object System.Collections.Generic.List[object]
foreach ($item in $logs) {
  $name = "$(Get-ComponentName $item) $($item.Operation) $($item.Service)"
  if ($name -match "(?i)ollama|qwen|piper|ecdict" -and ($item.Operation -or $item.DurationMs)) {
    $downstream.Add([pscustomobject]@{
      Name = "$(Get-ComponentName $item)/$($item.Operation)"
      Result = $item.RawResult
      DurationMs = $item.DurationMs
      Failed = Is-FailureLog $item
    })
  }
}
foreach ($item in $traces) {
  $name = "$($item.Component) $($item.Name)"
  if ($name -match "(?i)ollama|qwen|piper|ecdict") {
    $operationName = $item.Name -replace ("^{0}\." -f [regex]::Escape($item.Component)), ""
    $downstream.Add([pscustomobject]@{
      Name = "$($item.Component)/$operationName"
      Result = if ($item.Error -or $item.Status -match '"ERROR"') { "error" } else { "success" }
      DurationMs = $item.DurationMs
      Failed = ($item.Error -or $item.Status -match '"ERROR"')
    })
  }
}
$downstream = @(
  $downstream |
    Group-Object Name |
    ForEach-Object {
      $_.Group |
        Sort-Object @{ Expression = { if ($_.Failed) { 0 } else { 1 } } }, @{ Expression = { try { [double]$_.DurationMs } catch { 0 } }; Descending = $true } |
        Select-Object -First 1
    } |
    Sort-Object @{ Expression = { if ($_.Failed) { 0 } else { 1 } } }, @{ Expression = { try { [double]$_.DurationMs } catch { 0 } }; Descending = $true } |
    Select-Object -First 4
)

Write-Host "REQUEST SUMMARY" -ForegroundColor Cyan
Write-Host ("Request ID:           {0}" -f $RequestId)
Write-Host ("Endpoint:             {0}" -f $methodPath)
Write-Host ("HTTP Status:          {0}" -f $httpStatus)
Write-Host ("Total Duration:       {0}" -f $totalDuration)
Write-Host ("Result:               {0}" -f $statusText) -ForegroundColor $(if ($failed) { "Red" } else { "Green" })
Write-Host ("Error Type:           {0}" -f $(if ($errorType) { $errorType } else { "none" }))
Write-Host ("Likely Component:     {0}" -f $failureComponent)
Write-Host ("Key Error:            {0}" -f $keyError)
if ($downstream.Count -eq 0) {
  Write-Host "Downstream:           none recorded"
} else {
  $first = $true
  foreach ($item in $downstream) {
    $label = if ($first) { "Downstream:" } else { "" }
    $duration = if ($item.DurationMs) { "$($item.DurationMs) ms" } else { "duration n/a" }
    Write-Host ("{0,-22} {1} result={2} duration={3}" -f $label, $item.Name, $item.Result, $duration)
    $first = $false
  }
}
Write-Host ("Trace ID:             {0}" -f $(if ($traceId) { $traceId } else { "not recorded" }))
Write-Host "Metrics:              use /metrics for aggregate service status"

if ($Full) {
  Write-Host ""
  Write-Host "=== LOGS ===" -ForegroundColor Cyan
  if ($logs.Count -eq 0) {
    Write-Host "No Logs found"
  } else {
    foreach ($item in $logs) {
      Write-Host ("[{0}] level={1} event={2} operation={3} request_id={4} trace_id={5} span_id={6} service={7} component={8} duration_ms={9} error_type={10} error={11} source={12}" -f $item.Timestamp, $item.Level, $item.Event, $item.Operation, $item.RequestId, $item.TraceId, $item.SpanId, $item.Service, $item.Component, $item.DurationMs, $item.ErrorType, (TextOf $item.Error), $item.Source)
    }
  }
  Write-Host ""
  Write-Host "=== TRACE / SPAN ===" -ForegroundColor Cyan
  if ($traces.Count -eq 0) {
    Write-Host "No Trace / Span found"
  } else {
    foreach ($item in $traces) {
      Write-Host ("[{0}] name={1} trace_id={2} span_id={3} status={4} attributes={5} source={6}" -f $item.Timestamp, $item.Name, $item.TraceId, $item.SpanId, $item.Status, $item.Attributes, $item.Source)
    }
  }
}
