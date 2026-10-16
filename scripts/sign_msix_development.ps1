param(
    [Parameter(Mandatory = $true)][string]$Package,
    [Parameter(Mandatory = $true)][string]$OutputDirectory
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Add-Type -AssemblyName System.IO.Compression.FileSystem

$source = (Resolve-Path -LiteralPath $Package).Path
$archive = [IO.Compression.ZipFile]::OpenRead($source)
try {
    $metadataEntry = $archive.GetEntry('store-build.json')
    if ($null -eq $metadataEntry) { throw 'Missing build metadata.' }
    $reader = [IO.StreamReader]::new($metadataEntry.Open())
    try { $metadata = $reader.ReadToEnd() | ConvertFrom-Json } finally { $reader.Dispose() }
    $manifestEntry = $archive.GetEntry('AppxManifest.xml')
    $reader = [IO.StreamReader]::new($manifestEntry.Open())
    try { [xml]$manifest = $reader.ReadToEnd() } finally { $reader.Dispose() }
    if ($metadata.channel -ne 'development' -or
        $metadata.identity_name -ne 'DRYAPPTR.QuotaCrew.Development' -or
        $metadata.publisher -ne 'CN=QuotaCrew Development' -or
        $manifest.Package.Identity.Name -ne $metadata.identity_name -or
        $manifest.Package.Identity.Publisher -ne $metadata.publisher) {
        throw 'This script signs only the explicit QuotaCrew development identity.'
    }
    if ($null -ne $archive.GetEntry('AppxSignature.p7x')) { throw 'Source is already signed.' }
} finally { $archive.Dispose() }

$output = [IO.Path]::GetFullPath($OutputDirectory)
if (Test-Path -LiteralPath $output) { throw 'Choose a new output directory; existing files are never replaced.' }
$sdk = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits/10/bin'
$tool = Get-ChildItem -LiteralPath $sdk -Directory |
    Where-Object { $_.Name -match '^10\.\d+\.\d+\.\d+$' } |
    Sort-Object { [version]$_.Name } -Descending |
    ForEach-Object { Join-Path $_.FullName 'x64/signtool.exe' } |
    Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $tool) { throw 'Windows SDK SignTool is required.' }
New-Item -ItemType Directory -Path $output | Out-Null
$signed = Join-Path $output ([IO.Path]::GetFileName($source))
Copy-Item -LiteralPath $source -Destination $signed
$certificate = $null
try {
    $certificate = New-SelfSignedCertificate -Type Custom -Subject $metadata.publisher `
        -FriendlyName ('QuotaCrew temporary development signing ' + [guid]::NewGuid().ToString('N')) `
        -KeyAlgorithm RSA -KeyLength 2048 -HashAlgorithm SHA256 -KeyUsage DigitalSignature `
        -KeyExportPolicy NonExportable -NotAfter (Get-Date).AddDays(30) `
        -CertStoreLocation 'Cert:\CurrentUser\My' `
        -TextExtension @('2.5.29.37={text}1.3.6.1.5.5.7.3.3', '2.5.29.19={text}')
    & $tool sign /fd SHA256 /s My /sha1 $certificate.Thumbprint $signed
    if ($LASTEXITCODE -ne 0) { throw 'SignTool could not sign the development package.' }
    Add-Type -AssemblyName System.Security
    $signedArchive = [IO.Compression.ZipFile]::OpenRead($signed)
    $originalArchive = [IO.Compression.ZipFile]::OpenRead($source)
    try {
        $signature = $signedArchive.GetEntry('AppxSignature.p7x')
        if ($null -eq $signature) { throw 'Signature file missing after signing.' }
        $stream = $signature.Open()
        $buffer = [IO.MemoryStream]::new()
        try { $stream.CopyTo($buffer); $bytes = $buffer.ToArray() } finally { $stream.Dispose(); $buffer.Dispose() }
        if ([Text.Encoding]::ASCII.GetString($bytes, 0, 4) -ne 'PKCX') { throw 'Invalid AppX signature prefix.' }
        $cms = [Security.Cryptography.Pkcs.SignedCms]::new()
        $cms.Decode($bytes[4..($bytes.Length - 1)])
        $cms.CheckSignature($true)
        if ($cms.SignerInfos.Count -ne 1 -or $cms.SignerInfos[0].Certificate.Thumbprint -ne $certificate.Thumbprint) {
            throw 'Unexpected signing certificate.'
        }
        foreach ($entry in $originalArchive.Entries) {
            $other = $signedArchive.GetEntry($entry.FullName)
            if ($null -eq $other) { throw 'Signing removed a package entry.' }
            # Signing legitimately updates package footprint content types.
            if ($entry.FullName -eq '[Content_Types].xml') { continue }
            $left = $entry.Open(); $right = $other.Open()
            $hash = [Security.Cryptography.SHA256]::Create()
            try {
                $a = [Convert]::ToBase64String($hash.ComputeHash($left))
                $b = [Convert]::ToBase64String($hash.ComputeHash($right))
                if ($a -ne $b) { throw 'Signing changed a payload entry.' }
            } finally { $left.Dispose(); $right.Dispose(); $hash.Dispose() }
        }
    } finally { $signedArchive.Dispose(); $originalArchive.Dispose() }
    Export-Certificate -Cert $certificate -FilePath (Join-Path $output 'QuotaCrew-Development.cer') | Out-Null
    $digest = (Get-FileHash -LiteralPath $signed -Algorithm SHA256).Hash.ToLowerInvariant()
    [IO.File]::WriteAllText($signed + '.sha256', "$digest  $([IO.Path]::GetFileName($signed))`n", [Text.UTF8Encoding]::new($false))
    [ordered]@{
        package = $signed
        channel = 'development'
        certificate_subject = $certificate.Subject
        certificate_thumbprint = $certificate.Thumbprint
        expires = $certificate.NotAfter.ToUniversalTime().ToString('o')
        private_key_exported = $false
        host_trust_added = $false
        cms_signature_verified = $true
        payload_matches_unsigned_source = $true
        trusted_installation_verified = $false
        store_certified = $false
        sha256 = $digest
    } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $output 'signing.json') -Encoding utf8
} finally {
    # Delete only the ephemeral certificate created above, including its private key.
    if ($null -ne $certificate) {
        Remove-Item -LiteralPath ('Cert:\CurrentUser\My\' + $certificate.Thumbprint) -DeleteKey
    }
}
Write-Output "Signed development copy: $signed"
Write-Output 'No trust store was changed. For installation, trust the exported public certificate only in the isolated test environment.'
