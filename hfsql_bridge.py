"""
Bridge Python -> PowerShell -> ODBC HFSQL.
Contourne l'incompatibilité pyodbc / Python 3.14.
"""
import subprocess
import json
import tempfile
import os

DSN = "TEST-HFSQL-CODIAL"
UID = "admin"
PWD = "5f$ts4w!"

_PS_TEMPLATE = """\
$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'Stop'
try {{
    $conn = New-Object System.Data.Odbc.OdbcConnection('DSN={dsn};UID={uid};PWD={pwd};')
    $conn.Open()

    if ('{mode}' -eq 'tables') {{
        $dt = $conn.GetSchema('Tables')
        $tables = @()
        foreach ($row in $dt.Rows) {{ $tables += $row['TABLE_NAME'] }}
        Write-Output ($tables | ConvertTo-Json -Compress)
    }} elseif ('{mode}' -eq 'query') {{
        $cmd = $conn.CreateCommand()
        $cmd.CommandText = {query_json}
        $reader = $cmd.ExecuteReader()
        $cols = @()
        for ($i = 0; $i -lt $reader.FieldCount; $i++) {{ $cols += $reader.GetName($i) }}
        $rows = [System.Collections.Generic.List[hashtable]]::new()
        while ($reader.Read()) {{
            $row = @{{}}
            for ($i = 0; $i -lt $reader.FieldCount; $i++) {{
                $val = $reader.GetValue($i)
                if ($val -is [System.DBNull]) {{ $val = $null }}
                $row[$cols[$i]] = if ($null -eq $val) {{ $null }} else {{ "$val" }}
            }}
            $rows.Add($row)
        }}
        $reader.Close()
        if ($rows.Count -eq 0) {{ Write-Output '[]' }}
        else {{ Write-Output ($rows | ConvertTo-Json -Compress -Depth 3) }}
    }}
    $conn.Close()
}} catch {{
    Write-Output ((@{{ error = $_.Exception.Message }}) | ConvertTo-Json -Compress)
    exit 1
}}
"""


def _run_ps(mode, sql="", timeout=120):
    script = _PS_TEMPLATE.format(
        dsn=DSN,
        uid=UID,
        pwd=PWD,
        mode=mode,
        query_json=json.dumps(sql),
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".ps1",
                                     delete=False, encoding="utf-8") as f:
        f.write(script)
        tmp_path = f.name
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-File", tmp_path],
            capture_output=True, text=True, encoding="utf-8", timeout=timeout
        )
    finally:
        os.unlink(tmp_path)

    raw = result.stdout.strip()
    if not raw:
        raise RuntimeError(result.stderr.strip() or "Aucune réponse PowerShell")
    data = json.loads(raw)
    if isinstance(data, dict) and "error" in data:
        raise RuntimeError(data["error"])
    return data


def list_tables():
    """Retourne la liste des noms de tables de la base."""
    data = _run_ps("tables")
    if isinstance(data, str):
        return [data]
    return data if isinstance(data, list) else []


def query(sql, timeout=120):
    """Exécute une requête SELECT et retourne une liste de dicts."""
    data = _run_ps("query", sql, timeout=timeout)
    if not data:
        return []
    if isinstance(data, dict):
        return [data]
    return data


def test_connection():
    """Vérifie que la connexion fonctionne. Retourne True ou lève RuntimeError."""
    list_tables()
    return True
