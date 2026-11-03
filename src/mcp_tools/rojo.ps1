<#
  Rojo server and plugin helper (the mcp-tools server calls it; it works on its own too).
    rojo.ps1 start  -Project <default.project.json> [-Port 34872]  serve it in a process that outlives the caller
    rojo.ps1 status -Project <default.project.json> [-Port 34872]  is a server for this project answering?
    rojo.ps1 stop [-Port 34872]    stop the server this script started (never one it did not start)
    rojo.ps1 build                 build the auto-connect Rojo plugin into <StateDir>\out\Rojo.rbxm
  Pid files, logs and the plugin build go in -StateDir (default: next to this script).
#>
param(
    [Parameter(Mandatory = $true)][ValidateSet('start', 'status', 'stop', 'build')][string]$Command,
    [int]$Port = 34872,
    [string]$Project = 'default.project.json',
    [string]$StateDir = $PSScriptRoot
)
$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
New-Item -ItemType Directory -Force $StateDir | Out-Null
$rojo = if (Test-Path "$env:USERPROFILE\.aftman\bin\rojo.exe") { "$env:USERPROFILE\.aftman\bin\rojo.exe" } else { 'rojo' }
if ($Command -in 'start', 'status') {
    $projectFile = (Resolve-Path $Project).Path
    $repo = Split-Path $projectFile
    $project = (Get-Content $projectFile -Raw | ConvertFrom-Json).name
}
$pidFile = "$StateDir\serve-$Port.pid"
$logFile = "$StateDir\serve-$Port.log"

function Test-Server {
    # /api/rojo answers in msgpack; the project name sits in it as plain bytes
    try {
        $wc = New-Object System.Net.WebClient
        $body = [Text.Encoding]::ASCII.GetString($wc.DownloadData("http://localhost:$Port/api/rojo"))
        if ($body.Contains($project)) { return 'ours' }
        return 'other'
    } catch { return $null }
}

function Get-OurPid {
    if (-not (Test-Path $pidFile)) { return $null }
    $id = [int](Get-Content $pidFile)
    if (Get-Process -Id $id -ErrorAction SilentlyContinue) { return $id }
    Remove-Item $pidFile
    return $null
}

switch ($Command) {
    'status' {
        $s = Test-Server; $id = Get-OurPid
        if ($s -eq 'ours') { "running: '$project' on port $Port" + $(if ($id) { " (started by this script, pid $id)" } else { ' (not started by this script)' }) }
        elseif ($s -eq 'other') { "port $Port is serving a different Rojo project" ; exit 2 }
        else { "not running on port $Port"; exit 1 }
    }
    'start' {
        $s = Test-Server
        if ($s -eq 'ours') { "already running: '$project' on port $Port"; break }
        if ($s -eq 'other') { throw "port $Port is serving a different Rojo project" }
        # Win32_Process.Create spawns from the WMI service, so the server is not in the caller's
        # process tree or job object and survives the calling shell being killed.
        $cmdLine = "cmd.exe /d /c `"`"$rojo`" serve `"$projectFile`" --port $Port > `"$logFile`" 2>&1`""
        $r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmdLine; CurrentDirectory = $repo }
        if ($r.ReturnValue -ne 0) { throw "Win32_Process.Create failed: $($r.ReturnValue)" }
        Set-Content $pidFile $r.ProcessId
        for ($i = 0; $i -lt 40; $i++) {
            Start-Sleep -Milliseconds 250
            if ((Test-Server) -eq 'ours') { "started: '$project' on port $Port (pid $($r.ProcessId), log $logFile)"; exit 0 }
            if (-not (Get-Process -Id $r.ProcessId -ErrorAction SilentlyContinue)) { break }
        }
        Get-Content $logFile -ErrorAction SilentlyContinue
        throw 'rojo serve did not come up; log above'
    }
    'stop' {
        $id = Get-OurPid
        if (-not $id) { "no server started by this script on port $Port"; break }
        taskkill /T /F /PID $id | Out-Null
        Remove-Item $pidFile
        "stopped pid $id"
    }
    'build' {
        # Plugin is built from the tag matching the installed CLI, so plugin and server versions always match.
        $ver = ((& $rojo --version) -split ' ')[-1]
        $src = "$StateDir\build\rojo-$ver"
        if (-not (Test-Path $src)) {
            git clone -q --depth 1 --branch "v$ver" --recurse-submodules --shallow-submodules https://github.com/rojo-rbx/rojo.git $src
            if ($LASTEXITCODE) { throw 'clone failed' }
            git -C $src apply "$here\autoconnect.patch"
            if ($LASTEXITCODE) { throw "autoconnect.patch does not apply to Rojo $ver; port it by hand" }
        }
        New-Item -ItemType Directory -Force "$StateDir\out" | Out-Null
        & $rojo build "$src\plugin.project.json" -o "$StateDir\out\Rojo.rbxm"
        if ($LASTEXITCODE) { throw 'rojo build failed' }
        "built $StateDir\out\Rojo.rbxm (Rojo $ver + autoconnect.patch)"
    }
}
