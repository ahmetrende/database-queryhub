<#
.SYNOPSIS
    Break glass: shut every QueryHub database login out of the whole fleet.

.DESCRIPTION
    The laptop copy of scripts/breakglass_lockout.py. Same three moves, same
    order, no Python and no repo needed - PowerShell and psql are enough.

    Per login, on every target:

      1. NOLOGIN      stops NEW connections. First, because anything else
                      leaves a window in which a client reconnects behind you.
      2. new password invalidates the leaked secret itself. A NOLOGIN role can
                      be flipped back by anyone who can ALTER ROLE; the
                      password is what an attacker actually holds.
      3. terminate    kills the sessions already open. Last, because a session
                      killed before step 1 simply reconnects.

    The new passwords are random and are NOT stored. Severing access and
    rotating credentials are different jobs; recovery is meant to be
    deliberate - set fresh credentials on the targets afterwards and save them
    through QueryHub's admin path.

    It connects as YOUR superuser, never as QueryHub's own login, so nothing
    the bot holds is used to perform the lockout. The fleet list comes from a
    plan file exported on the bot host:

        python3 scripts/breakglass_lockout.py --dump-plan fleet.json

    That file holds hosts, ports and login names. No passwords, no key
    material. Re-export it after onboarding a server.

.EXAMPLE
    .\breakglass_lockout.ps1 -AdminUser postgres
    Dry run over the whole fleet: what would be locked, and how many sessions
    are open right now. Changes nothing.

.EXAMPLE
    .\breakglass_lockout.ps1 -AdminUser postgres -Apply
    Do it.

.EXAMPLE
    .\breakglass_lockout.ps1 -AdminUser postgres -Apply -Alias 'svc-prod-*'
    One slice of the fleet.

.NOTES
    Needs network reach to the databases (VPN if they are private) and psql on
    PATH. Pausing the bot itself is not done from here - that writes to the
    metadata DB. Use /sql kill on in Slack.
#>
[CmdletBinding()]
param(
    # The plan file exported by the Python script's --dump-plan.
    [string] $Plan = 'fleet.json',

    # Your own superuser on the targets. Never QueryHub's login.
    [Parameter(Mandatory = $true)] [string] $AdminUser,

    # Without this it is a dry run and touches nothing.
    [switch] $Apply,

    # Only targets whose alias matches this wildcard.
    [string] $Alias,

    # Leave open sessions running (rare: a long job that must finish).
    [switch] $NoTerminate,

    [int] $TimeoutSec = 8
)

$ErrorActionPreference = 'Stop'

# 64 characters exactly, so the byte-to-character mapping below has no modulo
# bias. Nothing here needs quoting inside a SQL literal.
$script:Alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_'

function New-LockoutPassword {
    <# A password nobody will ever see: long, random, never written down. #>
    $bytes = [byte[]]::new(40)
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    -join ($bytes | ForEach-Object { $script:Alphabet[$_ % 64] })
}

function Find-Psql {
    <# PATH first, then the usual Windows install directories, so a DBA who
       has pgAdmin but never edited PATH is not stuck mid-incident. #>
    $cmd = Get-Command psql -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $globs = @("$env:ProgramFiles\PostgreSQL\*\bin\psql.exe",
               "${env:ProgramFiles(x86)}\PostgreSQL\*\bin\psql.exe")
    foreach ($g in $globs) {
        $hit = Get-ChildItem $g -ErrorAction SilentlyContinue |
               Sort-Object FullName -Descending | Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    throw "psql not found. Install the PostgreSQL client tools, or add psql to PATH."
}

function Get-LoginNames($t) {
    <# The logins this target names, de-duplicated, in tier order. Read from
       the plan rather than assumed: a target may name its own users, and
       missing one is the failure that matters. #>
    @($t.username, $t.username_rw, $t.username_ddl) |
        Where-Object { $_ } | Select-Object -Unique
}

function Invoke-Psql($psql, $t, $sql) {
    <# Run one SQL script through psql. The SQL goes in on stdin, never as an
       argument, so a generated password never lands in the process list or in
       a shell history. #>
    $args = @('--host', $t.host, '--port', $t.port, '--dbname', $t.database,
              '--username', $AdminUser, '--no-psqlrc', '--quiet',
              '--no-align', '--tuples-only', '-v', 'ON_ERROR_STOP=1')
    $out = ($sql | & $psql @args) 2>&1
    [pscustomobject]@{ Ok = ($LASTEXITCODE -eq 0); Text = ($out -join "`n") }
}

function Build-Sql($names, $doApply, $terminate) {
    $list = ($names | ForEach-Object { "'" + $_.Replace("'", "''") + "'" }) -join ','
    $sb = [System.Text.StringBuilder]::new()
    # The names, not a count: the report should say which logins were actually
    # on this server, not how many of the ones we asked about were.
    [void]$sb.AppendLine("SELECT 'ROLE=' || rolname FROM pg_roles WHERE rolname IN ($list);")
    if ($doApply) {
        foreach ($n in $names) {
            $pw = New-LockoutPassword
            # format(%I/%L) does the quoting, and the IF EXISTS makes a target
            # that does not have this login a no-op instead of an error that
            # stops the rest of the script.
            [void]$sb.AppendLine(@"
DO `$`$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$n') THEN
    EXECUTE format('ALTER ROLE %I NOLOGIN', '$n');
    EXECUTE format('ALTER ROLE %I PASSWORD %L', '$n', '$pw');
  END IF;
END `$`$;
"@)
        }
    }
    if ($terminate) {
        # After the door is shut, not before.
        $agg = if ($doApply) { 'count(pg_terminate_backend(pid))' } else { 'count(*)' }
        [void]$sb.AppendLine(@"
SELECT 'SESSIONS=' || $agg FROM pg_stat_activity
 WHERE usename IN ($list) AND pid <> pg_backend_pid();
"@)
    }
    $sb.ToString()
}

# --- run ---------------------------------------------------------------------

$psql = Find-Psql
if (-not (Test-Path $Plan)) {
    throw "Plan file not found: $Plan. Export it on the bot host with: " +
          "python3 scripts/breakglass_lockout.py --dump-plan fleet.json"
}
$doc = Get-Content $Plan -Raw | ConvertFrom-Json
$targets = $doc.targets
if ($Alias) { $targets = $targets | Where-Object { $_.alias -like $Alias } }
if (-not $targets) { Write-Error "no targets matched"; exit 2 }

if (-not $env:PGPASSWORD) {
    $sec = Read-Host "Password for $AdminUser" -AsSecureString
    $env:PGPASSWORD = [System.Net.NetworkCredential]::new('', $sec).Password
}
$env:PGCONNECT_TIMEOUT = $TimeoutSec
$sslmode = if ($doc.ssl -and $doc.ssl.sslmode) { $doc.ssl.sslmode } else { 'require' }
$env:PGSSLMODE = $sslmode

$head = if ($Apply) { 'APPLYING' } else { 'DRY RUN (nothing will change)' }
$stamp = (Get-Date).ToUniversalTime().ToString('yyyy-MM-dd HH:mm:ss')
Write-Host "$head - $($targets.Count) target(s), $stamp UTC, from $Plan"
$last = if ($NoTerminate) { 'sessions left alone' } else { 'terminate sessions' }
Write-Host "  per login: NOLOGIN -> new random password -> $last"
Write-Host "  connecting as: $AdminUser (sslmode=$sslmode)`n"

$ok = 0; $failed = @(); $sessions = 0
try {
    foreach ($t in $targets) {
        $engine = if ($t.engine) { $t.engine } else { 'postgres' }
        if ($engine -ne 'postgres') {
            $failed += [pscustomobject]@{ Alias = $t.alias; Error = "engine '$engine' - lock this one by hand" }
            continue
        }
        $names = Get-LoginNames $t
        if (-not $names) {
            $failed += [pscustomobject]@{ Alias = $t.alias; Error = 'no QueryHub logins named on this target' }
            continue
        }
        $r = Invoke-Psql $psql $t (Build-Sql $names $Apply.IsPresent (-not $NoTerminate))
        if (-not $r.Ok) {
            $one = ($r.Text -split "`n" | Where-Object { $_ } | Select-Object -First 1)
            $failed += [pscustomobject]@{ Alias = $t.alias; Error = $one }
            continue
        }
        $found = @([regex]::Matches($r.Text, 'ROLE=(\S+)') | ForEach-Object { $_.Groups[1].Value })
        $killed = 0
        $m = [regex]::Match($r.Text, 'SESSIONS=(\d+)')
        if ($m.Success) { $killed = [int]$m.Groups[1].Value; $sessions += $killed }
        $verb = if ($Apply) { 'locked' } else { 'would lock' }
        $what = if ($found) { $found -join ', ' } else { '(none found)' }
        $line = "  {0,-28} {1}: {2}" -f $t.alias, $verb, $what
        $missing = @($names | Where-Object { $_ -notin $found })
        if ($missing) { $line += "  (not on this server: $($missing -join ', '))" }
        if ($killed) { $line += "  - sessions: $killed" }
        Write-Host $line
        $ok++
    }
}
finally {
    Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
}

if ($failed) {
    Write-Host "`n  FAILED:"
    foreach ($f in $failed) { Write-Host ("    {0,-28} {1}" -f $f.Alias, $f.Error) }
}
$word = if ($Apply) { 'terminated' } else { 'open right now' }
Write-Host "`n  $ok/$($targets.Count) target(s) done, $sessions session(s) $word"
if ($failed) {
    Write-Host "  A target that failed is a target still reachable with the old credentials."
    Write-Host "  Fix those by hand before you call this finished."
}
if ($Apply) {
    Write-Host "`n  The new passwords were not stored anywhere. To bring QueryHub back, set"
    Write-Host "  fresh credentials on each target and save them through the admin UI."
}
exit $(if ($failed) { 1 } else { 0 })
