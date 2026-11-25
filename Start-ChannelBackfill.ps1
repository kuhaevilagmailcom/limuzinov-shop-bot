$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $projectDir

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    $python = Get-Command py -ErrorAction SilentlyContinue
}
if (-not $python) {
    throw "Python 3.11+ не найден. Установи Python и повтори запуск."
}

$databasePath = Read-Host "Полный путь к suggestions.sqlite3"
if (-not (Test-Path -LiteralPath $databasePath -PathType Leaf)) {
    throw "Файл базы не найден: $databasePath"
}

$moderationChannel = Read-Host "ID админской группы (например -5106639548)"
$publicationChannel = Read-Host "ID канала с постами (полный -100...)"
$apiId = Read-Host "TELEGRAM_API_ID с my.telegram.org"
$apiHashSecure = Read-Host "TELEGRAM_API_HASH с my.telegram.org" -AsSecureString
$apiHashPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($apiHashSecure)
try {
    $env:TELEGRAM_API_ID = $apiId
    $env:TELEGRAM_API_HASH = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($apiHashPointer)

    Write-Host "Устанавливаю/проверяю зависимости..."
    & $python.Source -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "Не удалось установить зависимости." }

    Write-Host "Сначала выполняю безопасную проверку без записи в БД..."
    & $python.Source channel_backfill.py `
        --moderation-channel $moderationChannel `
        --publication-channel $publicationChannel `
        --db $databasePath `
        --dry-run
    $dryRunCode = $LASTEXITCODE
    if ($dryRunCode -notin @(0, 2)) { throw "Проверочный проход завершился с ошибкой $dryRunCode." }

    $confirm = Read-Host "Записать найденные публикации в БД? Напиши ДА"
    if ($confirm -cne "ДА") {
        Write-Host "Запись отменена. База не изменена."
        exit 0
    }

    & $python.Source channel_backfill.py `
        --moderation-channel $moderationChannel `
        --publication-channel $publicationChannel `
        --db $databasePath
    exit $LASTEXITCODE
}
finally {
    if ($apiHashPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($apiHashPointer)
    }
    Remove-Item Env:TELEGRAM_API_ID -ErrorAction SilentlyContinue
    Remove-Item Env:TELEGRAM_API_HASH -ErrorAction SilentlyContinue
}
