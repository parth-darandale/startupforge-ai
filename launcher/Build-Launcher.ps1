param([switch]$RunChecks)
$ErrorActionPreference = 'Stop'
$workspaceDirectory = Split-Path -Parent $PSScriptRoot
$compilerExecutable = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (!(Test-Path -LiteralPath $compilerExecutable)) {
    $compilerExecutable = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe'
}
if (!(Test-Path -LiteralPath $compilerExecutable)) {
    throw 'The Windows .NET Framework C# compiler was not found. Enable/install .NET Framework 4.8 and rebuild.'
}
$sourceFile = Join-Path $PSScriptRoot 'StartupForgeLauncher.cs'
$outputFile = Join-Path $workspaceDirectory 'StartupForge.exe'
& $compilerExecutable /nologo /target:winexe /platform:anycpu /optimize+ /debug- /utf8output "/out:$outputFile" /reference:System.dll /reference:System.Core.dll /reference:System.Drawing.dll /reference:System.Windows.Forms.dll /reference:System.Web.Extensions.dll $sourceFile
if ($LASTEXITCODE -ne 0) { throw 'StartupForge launcher compilation failed.' }
Write-Output "Built $outputFile"
if ($RunChecks) {
    $reportFile = Join-Path $PSScriptRoot '.runtime\check-result.json'
    $check = Start-Process -FilePath $outputFile -ArgumentList @('--check') -WindowStyle Hidden -Wait -PassThru
    Get-Content -LiteralPath $reportFile
    if ($check.ExitCode -ne 0) { throw 'The launcher workspace check failed; inspect the report above.' }
}
