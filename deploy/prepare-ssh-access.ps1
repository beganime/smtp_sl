param(
    [string]$KeyPath = "$HOME\.ssh\smtp_sl_codex_ed25519",
    [string]$ServerIp = "95.163.226.244",
    [int]$Port = 22
)

$ErrorActionPreference = "Stop"
$sshDirectory = Split-Path -Parent $KeyPath
if (-not (Test-Path -LiteralPath $sshDirectory)) {
    New-Item -ItemType Directory -Path $sshDirectory | Out-Null
}

if (-not (Test-Path -LiteralPath $KeyPath)) {
    & ssh-keygen -t ed25519 -a 100 -N '""' -C "smtp-sl-deploy@tmmail.ru" -f $KeyPath
    if ($LASTEXITCODE -ne 0) {
        throw "Could not create the SSH key. Make sure OpenSSH is installed."
    }
}

$publicKeyPath = "$KeyPath.pub"
if (-not (Test-Path -LiteralPath $publicKeyPath)) {
    throw "Public key was not found: $publicKeyPath"
}

$publicKey = (Get-Content -LiteralPath $publicKeyPath -Raw).Trim()
$portOpen = Test-NetConnection -ComputerName $ServerIp -Port $Port -InformationLevel Quiet

Write-Host ""
Write-Host "SMTP_SL SSH key is ready." -ForegroundColor Green
Write-Host "Private key: $KeyPath (do not share it)"
Write-Host "Public key: $publicKeyPath"
Write-Host "SSH port is reachable: $portOpen"
Write-Host ""
Write-Host "Add only this line to the server user's ~/.ssh/authorized_keys:" -ForegroundColor Cyan
Write-Output $publicKey
Write-Host ""
Write-Host "After adding it, provide the SSH username and a custom port if it is not 22."
Write-Host "Access test:"
Write-Host "ssh -i `"$KeyPath`" -p $Port USER@$ServerIp"
