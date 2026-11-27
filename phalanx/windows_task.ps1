$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class PhalanxTaskArguments {
    [DllImport("shell32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    public static extern IntPtr CommandLineToArgvW(string command, out int count);
    [DllImport("kernel32.dll")]
    public static extern IntPtr LocalFree(IntPtr memory);
}
'@

function Get-Hash([string] $Value) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($sha.ComputeHash(
            [Text.Encoding]::UTF8.GetBytes($Value)))).Replace('-', '').ToLowerInvariant()
    } finally { $sha.Dispose() }
}

function Get-ArgumentsHash([string] $Command) {
    if ([string]::IsNullOrWhiteSpace($Command)) { return $null }
    $count = 0
    $pointer = [PhalanxTaskArguments]::CommandLineToArgvW($Command, [ref] $count)
    if ($pointer -eq [IntPtr]::Zero) { throw 'Cannot parse process command identity' }
    try {
        $arguments = @()
        for ($index = 1; $index -lt $count; $index++) {
            $item = [Runtime.InteropServices.Marshal]::ReadIntPtr($pointer, $index * [IntPtr]::Size)
            $arguments += [Runtime.InteropServices.Marshal]::PtrToStringUni($item)
        }
        return Get-Hash ($arguments -join "`0")
    } finally { $null = [PhalanxTaskArguments]::LocalFree($pointer) }
}

function Get-ProcessIdentity([uint32] $ProcessId) {
    $rows = @(Get-CimInstance Win32_Process -Filter ('ProcessId=' + $ProcessId) -ErrorAction Stop)
    foreach ($row in $rows) {
        $created = $null
        if ($row.CreationDate -is [DateTime]) {
            $created = $row.CreationDate.ToUniversalTime().ToFileTimeUtc()
        }
        $executable = $null
        if (-not [string]::IsNullOrWhiteSpace($row.ExecutablePath)) {
            $executable = Get-Hash ([IO.Path]::GetFullPath($row.ExecutablePath).ToLowerInvariant())
        }
        [ordered]@{
            pid = [long] $row.ProcessId
            parent = [long] $row.ParentProcessId
            created = $created
            executable_hash = $executable
            arguments_hash = Get-ArgumentsHash $row.CommandLine
        }
    }
}

function Get-TaskProof($Task, $Instances) {
    if ($Task.Definition.Actions.Count -ne 1 -or $Task.Definition.Actions.Item(1).Type -ne 0) {
        throw 'Exactly one existing executable task action is required'
    }
    if ($Instances.Count -ne 1) { throw 'Original task instance is missing or ambiguous' }
    $action = $Task.Definition.Actions.Item(1)
    $executable = [Environment]::ExpandEnvironmentVariables($action.Path)
    if (-not [IO.Path]::IsPathRooted($executable)) { throw 'Task executable must be absolute' }
    return [ordered]@{
        definition_hash = Get-Hash $Task.Xml
        instance = [string] $Instances.Item(1).InstanceGuid
        engine_pid = [long] $Instances.Item(1).EnginePID
        action_executable_hash = Get-Hash ([IO.Path]::GetFullPath($executable).ToLowerInvariant())
        action_arguments_hash = Get-ArgumentsHash ('"' + $executable + '" ' + $action.Arguments)
    }
}

try {
    $request = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $result = @{}
    if ($request.operation -eq 'processes') {
        $result.processes = @(
            foreach ($processId in $request.pids) { Get-ProcessIdentity ([uint32] $processId) }
        )
    } else {
        $service = New-Object -ComObject 'Schedule.Service'
        $service.Connect()
        $taskPath = [string] $request.task
        if (-not $taskPath.StartsWith('\') -or $taskPath.EndsWith('\')) {
            throw 'An exact absolute existing task path is required'
        }
        $task = $service.GetFolder('\').GetTask($taskPath)
        $instances = $task.GetInstances(0)
        if ($request.operation -eq 'excluded') {
            $result.task_excluded = ($instances.Count -eq 0)
            $listeners = @(Get-NetTCPConnection -State Listen -ErrorAction Stop |
                Where-Object { $_.LocalPort -eq [int] $request.port })
            $result.listeners = @($listeners | ForEach-Object {
                @{ pid = [long] $_.OwningProcess; port = [int] $_.LocalPort }
            })
        } elseif ($request.operation -eq 'inspect' -or $request.operation -eq 'stop') {
            $proof = Get-TaskProof $task $instances
            if ($request.operation -eq 'stop') {
                foreach ($name in $proof.Keys) {
                    if ($proof[$name] -cne $request.expected.$name) {
                        throw 'Original task definition or instance changed; stop refused'
                    }
                }
                # Stop the exact running instance, never the shared task engine.
                $instances.Item(1).Stop()
            }
            $result.task = $proof
        } else { throw 'Unknown ownership operation' }
    }
    $result | ConvertTo-Json -Depth 12 -Compress
} catch {
    # Never echo task XML, action arguments, CIM command lines or provider error text.
    [Console]::Error.WriteLine('Windows task ownership operation failed: ' + $_.Exception.GetType().FullName)
    exit 1
}
