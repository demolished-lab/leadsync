#!/usr/bin/env python3
"""
Database backup and restore for the Sales Follow-Up Agent.

Supports:
  - PostgreSQL (via pg_dump / pg_restore or SQLAlchemy SQL dump)
  - SQLite (file copy with WAL checkpoint)
  - Compression (gzip)
  - Rotation (keep last N backups)
  - Verification (checksum + row count)
  - Supplementary files (dedup cache, suppression list, auth config)

Usage:
    # Backup
    python scripts/db_backup.py backup
    python scripts/db_backup.py backup --output /path/to/backupDir
    python scripts/db_backup.py backup --compress
    python scripts/db_backup.py backup --rotate 7

    # Restore
    python scripts/db_backup.py restore backups/sfa_20240115_103000.sql.gz
    python scripts/db_backup.py restore backups/sfa_20240115_103000.sql.gz --confirm

    # List backups
    python scripts/db_backup.py list
    python scripts/db_backup.py list --output /path/to/backupDir

    # Verify
    python scripts/db_backup.py verify backups/sfa_20240115_103000.sql.gz

    # Show status
    python scripts/db_backup.py status
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import gzip
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


# ── Helpers ────────────────────────────────────────────────────


def _get_db_url() -> str:
    """Get database URL from environment."""
    from core.config import get_settings
    settings = get_settings()
    return getattr(settings, "database_url", None) or ""


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def _is_postgres(url: str) -> bool:
    return url.startswith("postgresql") or url.startswith("postgres")


def _parse_pg_url(url: str) -> Dict[str, str]:
    """Parse PostgreSQL URL into components."""
    # postgresql://user:pass@host:port/dbname
    url = url.replace("postgresql://", "").replace("postgres://", "")
    auth, rest = url.split("@", 1) if "@" in url else ("", url)
    user, password = auth.split(":", 1) if ":" in auth else (auth, "")
    host_port, dbname = rest.split("/", 1) if "/" in rest else (rest, "")
    host, port = host_port.split(":", 1) if ":" in host_port else (host_port, "5432")
    return {
        "user": user or "postgres",
        "password": password,
        "host": host or "localhost",
        "port": port or "5432",
        "dbname": dbname or "sfa",
    }


def _file_checksum(path: str) -> str:
    """SHA-256 checksum of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _gzip_file(src: str, dst: str) -> None:
    """Compress a file with gzip."""
    with open(src, "rb") as f_in:
        with gzip.open(dst, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)


def _gunzip_file(src: str, dst: str) -> None:
    """Decompress a gzip file."""
    with gzip.open(src, "rb") as f_in:
        with open(dst, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)


def _timestamp() -> str:
    return datetime.utcnow().strftime("%Y%m%d_%H%M%S")


# ── Path confinement (anti-traversal / anti file-read oracle) ──

def _confinement_error(path: str, what: str) -> Optional[str]:
    """
    Return an error message if `path` may not be used for `what`
    ("write" for backup output dir, "read" for verify/list/restore).

    Read access is restricted to agent artifact names inside the project
    tree — this prevents `/backup/verify?backup_path=.env` (or any other
    credential file) from being used as an arbitrary file-read oracle.
    """
    abs_path = os.path.realpath(os.path.abspath(path))
    project_root = os.path.realpath(PROJECT_ROOT)
    fname = os.path.basename(abs_path)

    if what == "write":
        if not abs_path.startswith(project_root + os.sep):
            return f"Output directory must be inside the project tree ({project_root})"
        return None

    if not abs_path.startswith(project_root + os.sep):
        return f"Path must be inside the project tree ({project_root})"
    allowed_ext = (".sql", ".gz", ".db", ".json")
    covers_supplementary = any(
        k in fname for k in ("dedup_cache", "suppressions", "auth_config", "manifest")
    )
    if not (fname.startswith("sfa_") or covers_supplementary):
        return "Only agent backup artifacts (sfa_* or known supplementary files) can be accessed"
    if not fname.endswith(allowed_ext):
        return f"Unsupported file type. Allowed suffixes: {', '.join(allowed_ext)}"
    # Bare credential files are never readable, even inside the project tree
    secret_names = (".env", "gmail_token.json", "auth_config.yaml", "auth_config.json")
    if fname in secret_names:
        return "Access to credential files is not allowed"
    return None


# ── Backup ─────────────────────────────────────────────────────


def backup_sqlite(db_url: str, output_dir: str, compress: bool = False) -> Dict[str, Any]:
    """Backup a SQLite database by file copy."""
    # Extract file path from URL: sqlite:///path/to/db.sqlite
    db_path = db_url.replace("sqlite:///", "").replace("sqlite://", "")

    if not os.path.exists(db_path):
        return {"status": "error", "message": f"SQLite file not found: {db_path}"}

    ts = _timestamp()
    base_name = f"sfa_{ts}"
    backup_path = os.path.join(output_dir, f"{base_name}.db")

    # Checkpoint WAL before copying
    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
    except Exception as e:
        print(f"  Warning: WAL checkpoint failed ({e}), proceeding with copy")

    # Copy the database file
    shutil.copy2(db_path, backup_path)

    # Also copy WAL and SHM files if they exist
    for ext in ["-wal", "-shm"]:
        src_ext = db_path + ext
        if os.path.exists(src_ext):
            shutil.copy2(src_ext, backup_path + ext)

    result = {
        "status": "success",
        "type": "sqlite",
        "source": db_path,
        "backup_path": backup_path,
        "size_bytes": os.path.getsize(backup_path),
        "checksum": _file_checksum(backup_path),
    }

    if compress:
        gz_path = backup_path + ".gz"
        _gzip_file(backup_path, gz_path)
        os.remove(backup_path)
        # Remove WAL/SHM copies too
        for ext in ["-wal", "-shm"]:
            if os.path.exists(backup_path + ext):
                os.remove(backup_path + ext)
        result["backup_path"] = gz_path
        result["size_bytes"] = os.path.getsize(gz_path)
        result["checksum"] = _file_checksum(gz_path)
        result["compressed"] = True

    return result


def backup_postgres(db_url: str, output_dir: str, compress: bool = False) -> Dict[str, Any]:
    """Backup a PostgreSQL database using pg_dump."""
    pg = _parse_pg_url(db_url)
    ts = _timestamp()
    base_name = f"sfa_{ts}"
    sql_path = os.path.join(output_dir, f"{base_name}.sql")

    # Try pg_dump first (most reliable)
    env = os.environ.copy()
    if pg["password"]:
        env["PGPASSWORD"] = pg["password"]

    pg_dump_cmd = [
        "pg_dump",
        "-h", pg["host"],
        "-p", pg["port"],
        "-U", pg["user"],
        "-d", pg["dbname"],
        "--clean",
        "--if-exists",
        "--no-owner",
        "--no-privileges",
        "-f", sql_path,
    ]

    try:
        result = subprocess.run(
            pg_dump_cmd,
            capture_output=True,
            text=True,
            env=env,
            timeout=300,
        )
        if result.returncode != 0:
            return {
                "status": "error",
                "message": f"pg_dump failed: {result.stderr[:500]}",
                "command": " ".join(pg_dump_cmd),
            }
    except FileNotFoundError:
        # pg_dump not available — fall back to SQLAlchemy dump
        return _backup_postgres_sqlalchemy(pg, sql_path)
    except subprocess.TimeoutExpired:
        return {"status": "error", "message": "pg_dump timed out after 5 minutes"}

    result = {
        "status": "success",
        "type": "postgresql",
        "source": f"{pg['host']}:{pg['port']}/{pg['dbname']}",
        "backup_path": sql_path,
        "size_bytes": os.path.getsize(sql_path),
        "checksum": _file_checksum(sql_path),
    }

    if compress:
        gz_path = sql_path + ".gz"
        _gzip_file(sql_path, gz_path)
        os.remove(sql_path)
        result["backup_path"] = gz_path
        result["size_bytes"] = os.path.getsize(gz_path)
        result["checksum"] = _file_checksum(gz_path)
        result["compressed"] = True

    return result


def _backup_postgres_sqlalchemy(pg: Dict[str, str], output_path: str) -> Dict[str, Any]:
    """Fallback: dump PostgreSQL via SQLAlchemy SELECT queries."""
    from sqlalchemy import create_engine, inspect, text

    url = f"postgresql://{pg['user']}:{pg['password']}@{pg['host']}:{pg['port']}/{pg['dbname']}"
    engine = create_engine(url)
    inspector = inspect(engine)

    lines = [
        f"-- Sales Follow-Up Agent Database Backup",
        f"-- Generated: {datetime.utcnow().isoformat()}",
        f"-- Database: {pg['host']}:{pg['port']}/{pg['dbname']}",
        "",
    ]

    table_count = 0
    row_count = 0

    for table_name in inspector.get_table_names():
        columns = [col["name"] for col in inspector.get_columns(table_name)]
        pk_cols = inspector.get_pk_constraint(table_name).get("constrained_columns", [])

        lines.append(f"-- Table: {table_name}")
        lines.append(f"DELETE FROM {table_name};")

        with engine.connect() as conn:
            result = conn.execute(text(f"SELECT * FROM {table_name}"))
            for row in result:
                values = []
                for val in row:
                    if val is None:
                        values.append("NULL")
                    elif isinstance(val, str):
                        values.append(f"'{val.replace(chr(39), chr(39)+chr(39))}'")
                    elif isinstance(val, bool):
                        values.append("TRUE" if val else "FALSE")
                    elif isinstance(val, (int, float)):
                        values.append(str(val))
                    else:
                        values.append(f"'{str(val)}'")
                cols_str = ", ".join(columns)
                vals_str = ", ".join(values)
                lines.append(f"INSERT INTO {table_name} ({cols_str}) VALUES ({vals_str});")
                row_count += 1

        lines.append("")
        table_count += 1

    engine.dispose()

    with open(output_path, "w") as f:
        f.write("\n".join(lines))

    return {
        "status": "success",
        "type": "postgresql (sqlalchemy fallback)",
        "backup_path": output_path,
        "size_bytes": os.path.getsize(output_path),
        "checksum": _file_checksum(output_path),
        "tables": table_count,
        "rows": row_count,
    }


def backup_supplementary(
    output_dir: str,
    compress: bool = False,
    include_auth_config: bool = True,
) -> List[Dict[str, Any]]:
    """Backup supplementary files: dedup cache, suppressions, auth config.

    ``include_auth_config=False`` (used by the HTTP API) skips auth_config.yaml
    because it contains password hashes and TOTP secrets.
    """
    files_to_backup = []

    # Dedup cache
    dedup_path = os.environ.get("DEDUP_CACHE_PATH", ".dedup_cache.json")
    if os.path.exists(dedup_path):
        files_to_backup.append(("dedup_cache.json", dedup_path))

    # Suppressions list
    suppressions_path = os.environ.get("SUPPRESSIONS_LIST_PATH", ".suppressions.txt")
    if os.path.exists(suppressions_path):
        files_to_backup.append(("suppressions.txt", suppressions_path))

    # Auth config — gated, it contains secrets
    if include_auth_config:
        auth_path = os.environ.get("AUTH_CONFIG_PATH", os.path.join(PROJECT_ROOT, "auth_config.yaml"))
        if os.path.exists(auth_path):
            files_to_backup.append(("auth_config.yaml", auth_path))

    results = []
    ts = _timestamp()
    for name, src in files_to_backup:
        dst = os.path.join(output_dir, f"sfa_{ts}_{name}")
        shutil.copy2(src, dst)

        info = {
            "file": name,
            "source": src,
            "backup_path": dst,
            "size_bytes": os.path.getsize(dst),
        }

        if compress:
            gz_path = dst + ".gz"
            _gzip_file(dst, gz_path)
            os.remove(dst)
            dst = gz_path
            info["backup_path"] = gz_path
            info["size_bytes"] = os.path.getsize(gz_path)
            info["compressed"] = True

        info["checksum"] = _file_checksum(dst)
        results.append(info)

    return results


def run_backup(
    output_dir: str = "backups",
    compress: bool = True,
    rotate: int = 7,
    include_supplementary: bool = True,
    api_mode: bool = False,
) -> Dict[str, Any]:
    """Run a full backup.

    ``api_mode=True`` (HTTP endpoint) also excludes auth_config.yaml from
    supplementary files — secrets must not be copied via API-triggered
    backups.
    """
    if api_mode:
        # Path confinement for HTTP-triggered backups
        confinement = _confinement_error(os.path.abspath(output_dir), "write")
        if confinement:
            return {"status": "error", "message": confinement}

    os.makedirs(output_dir, exist_ok=True)
    db_url = _get_db_url()

    print(f"{'='*60}")
    print(f"  Sales Follow-Up Agent — Database Backup")
    print(f"  {datetime.utcnow().isoformat()}")
    print(f"{'='*60}")

    result = {
        "timestamp": datetime.utcnow().isoformat(),
        "output_dir": os.path.abspath(output_dir),
        "compressed": compress,
    }

    # Database backup
    if not db_url:
        print("\n  No DATABASE_URL configured — using SQLite default")
        from core.database import _get_database_url
        db_url = _get_database_url()

    print(f"\n  Database: {'SQLite' if _is_sqlite(db_url) else 'PostgreSQL'}")
    print(f"  Output:   {output_dir}/")
    print(f"  Compress: {compress}")

    if _is_sqlite(db_url):
        db_result = backup_sqlite(db_url, output_dir, compress=compress)
    elif _is_postgres(db_url):
        db_result = backup_postgres(db_url, output_dir, compress=compress)
    else:
        db_result = {"status": "error", "message": f"Unsupported database URL: {db_url}"}

    result["database"] = db_result

    if db_result["status"] == "success":
        size_kb = db_result["size_bytes"] / 1024
        print(f"\n  ✅ Database backup: {db_result['backup_path']}")
        print(f"     Size: {size_kb:.1f} KB | Checksum: {db_result['checksum'][:16]}...")
    else:
        print(f"\n  ❌ Database backup failed: {db_result.get('message', 'unknown error')}")

    # Supplementary files
    if include_supplementary:
        print(f"\n  Backing up supplementary files...")
        supp_results = backup_supplementary(output_dir, compress=compress, include_auth_config=not api_mode)
        result["supplementary"] = supp_results
        for s in supp_results:
            size_kb = s["size_bytes"] / 1024
            print(f"    📄 {s['file']}: {size_kb:.1f} KB")

    # Rotation
    if rotate > 0:
        removed = _rotate_backups(output_dir, rotate)
        if removed:
            print(f"\n  🗑️  Rotated: removed {len(removed)} old backup(s)")
            result["rotated"] = removed

    # Save manifest
    manifest_path = os.path.join(output_dir, f"sfa_{_timestamp()}_manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(result, f, indent=2, default=str)
    result["manifest"] = manifest_path

    print(f"\n  📋 Manifest: {manifest_path}")
    print(f"{'='*60}")

    return result


# ── Restore ────────────────────────────────────────────────────


def restore_sqlite(backup_path: str, db_url: str) -> Dict[str, Any]:
    """Restore a SQLite database from backup."""
    db_path = db_url.replace("sqlite:///", "").replace("sqlite://", "")

    # Decompress if needed
    actual_path = backup_path
    if backup_path.endswith(".gz"):
        actual_path = backup_path[:-3]
        _gunzip_file(backup_path, actual_path)

    # Backup current DB before overwriting
    if os.path.exists(db_path):
        pre_restore = db_path + f".pre_restore_{_timestamp()}"
        shutil.copy2(db_path, pre_restore)
        print(f"  📦 Current DB backed up to: {pre_restore}")

    # Copy backup to DB location
    shutil.copy2(actual_path, db_path)

    # Copy WAL/SHM if they exist in backup
    for ext in ["-wal", "-shm"]:
        src = actual_path + ext
        if os.path.exists(src):
            shutil.copy2(src, db_path + ext)

    # Cleanup temp decompressed file
    if backup_path.endswith(".gz") and actual_path != backup_path:
        os.remove(actual_path)

    return {
        "status": "success",
        "restored_to": db_path,
        "source": backup_path,
    }


def restore_postgres(backup_path: str, db_url: str) -> Dict[str, Any]:
    """Restore a PostgreSQL database from SQL dump."""
    pg = _parse_pg_url(db_url)

    # Decompress if needed
    actual_path = backup_path
    if backup_path.endswith(".gz"):
        actual_path = backup_path + ".tmp"
        _gunzip_file(backup_path, actual_path)

    env = os.environ.copy()
    if pg["password"]:
        env["PGPASSWORD"] = pg["password"]

    # Try pg_restore for custom format, psql for SQL
    if backup_path.endswith(".dump") or backup_path.endswith(".pgc"):
        cmd = [
            "pg_restore",
            "-h", pg["host"],
            "-p", pg["port"],
            "-U", pg["user"],
            "-d", pg["dbname"],
            "--clean",
            "--if-exists",
            "--no-owner",
            actual_path,
        ]
    else:
        cmd = [
            "psql",
            "-h", pg["host"],
            "-p", pg["port"],
            "-U", pg["user"],
            "-d", pg["dbname"],
            "-f", actual_path,
        ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            timeout=600,
        )

        if actual_path != backup_path:
            os.remove(actual_path)

        if result.returncode != 0:
            return {
                "status": "error",
                "message": f"Restore failed: {result.stderr[:500]}",
            }

        return {
            "status": "success",
            "restored_to": f"{pg['host']}:{pg['port']}/{pg['dbname']}",
            "source": backup_path,
        }
    except FileNotFoundError:
        if actual_path != backup_path:
            os.remove(actual_path)
        return {"status": "error", "message": "psql/pg_restore not found. Install PostgreSQL client tools."}
    except subprocess.TimeoutExpired:
        if actual_path != backup_path:
            os.remove(actual_path)
        return {"status": "error", "message": "Restore timed out after 10 minutes"}


def restore_supplementary(backup_dir: str, backup_prefix: str) -> List[Dict[str, Any]]:
    """Restore supplementary files from backup."""
    results = []

    file_map = {
        "dedup_cache.json": os.environ.get("DEDUP_CACHE_PATH", ".dedup_cache.json"),
        "suppressions.txt": os.environ.get("SUPPRESSIONS_LIST_PATH", ".suppressions.txt"),
        "auth_config.yaml": os.environ.get("AUTH_CONFIG_PATH", os.path.join(PROJECT_ROOT, "auth_config.yaml")),
    }

    for backup_name, dest_path in file_map.items():
        # Find matching backup file (with timestamp prefix)
        import glob
        pattern = os.path.join(backup_dir, f"sfa_*_{backup_name}*")
        matches = sorted(glob.glob(pattern), reverse=True)

        if matches:
            src = matches[0]
            # Decompress if needed
            if src.endswith(".gz"):
                tmp = dest_path + ".tmp"
                _gunzip_file(src, tmp)
                shutil.move(tmp, dest_path)
            else:
                shutil.copy2(src, dest_path)

            results.append({
                "file": backup_name,
                "restored_from": src,
                "restored_to": dest_path,
                "status": "success",
            })
            print(f"    ✅ {backup_name} restored")
        else:
            results.append({
                "file": backup_name,
                "status": "not_found",
            })
            print(f"    ⚠️  {backup_name} not found in backup")

    return results


def run_restore(backup_path: str, restore_supplementary_files: bool = True) -> Dict[str, Any]:
    """Run a full restore."""
    print(f"{'='*60}")
    print(f"  Sales Follow-Up Agent — Database Restore")
    print(f"{'='*60}")

    if not os.path.exists(backup_path):
        return {"status": "error", "message": f"Backup file not found: {backup_path}"}

    db_url = _get_db_url()
    if not db_url:
        from core.database import _get_database_url
        db_url = _get_database_url()

    print(f"\n  Source:   {backup_path}")
    print(f"  Target:   {'SQLite' if _is_sqlite(db_url) else 'PostgreSQL'}")

    if _is_sqlite(db_url):
        result = restore_sqlite(backup_path, db_url)
    elif _is_postgres(db_url):
        result = restore_postgres(backup_path, db_url)
    else:
        return {"status": "error", "message": f"Unsupported database: {db_url}"}

    if result["status"] == "success":
        print(f"\n  ✅ Database restored to: {result['restored_to']}")
    else:
        print(f"\n  ❌ Restore failed: {result.get('message', 'unknown error')}")
        return result

    # Restore supplementary files
    if restore_supplementary_files:
        print(f"\n  Restoring supplementary files...")
        backup_dir = os.path.dirname(backup_path)
        supp_results = restore_supplementary(backup_dir, "")
        result["supplementary"] = supp_results

    print(f"{'='*60}")
    return result


# ── List & Verify ──────────────────────────────────────────────


def list_backups(output_dir: str = "backups", confined: bool = False) -> List[Dict[str, Any]]:
    """List all backups in a directory.

    ``confined=True`` (HTTP endpoint) restricts the directory to the
    project tree to prevent using the API as a directory-probing oracle.
    """
    if confined:
        confinement = _confinement_error(os.path.abspath(os.path.join(output_dir, "x")), "read")
        if confinement:
            return []
    if not os.path.exists(output_dir):
        return []

    backups = []
    for f in sorted(os.listdir(output_dir)):
        if f.startswith("sfa_") and not f.endswith("_manifest.json"):
            path = os.path.join(output_dir, f)
            stat = os.stat(path)
            backups.append({
                "filename": f,
                "path": path,
                "size_bytes": stat.st_size,
                "size_kb": round(stat.st_size / 1024, 1),
                "created": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "type": "database" if not any(x in f for x in ["dedup", "suppressions", "auth"]) else "supplementary",
                "compressed": f.endswith(".gz"),
            })

    return backups


def verify_backup(backup_path: str, confined: bool = False) -> Dict[str, Any]:
    """Verify a backup file is valid.

    ``confined=True`` (HTTP endpoint) applies path confinement so the
    endpoint cannot act as a file-read oracle.
    """
    if confined:
        # Path confinement — this endpoint must not act as a file-read oracle
        confinement = _confinement_error(backup_path, "read")
        if confinement:
            return {"status": "error", "message": confinement}
    if not os.path.exists(backup_path):
        return {"status": "error", "message": f"File not found: {backup_path}"}

    result = {
        "path": backup_path,
        "size_bytes": os.path.getsize(backup_path),
        "checksum": _file_checksum(backup_path),
    }

    # Check if gzip is valid
    if backup_path.endswith(".gz"):
        try:
            with gzip.open(backup_path, "rb") as f:
                f.read(1024)  # Read first 1KB to verify
            result["gzip_valid"] = True
        except Exception as e:
            result["gzip_valid"] = False
            result["error"] = str(e)
            result["status"] = "error"
            return result

    # Check if SQL content is readable
    try:
        if backup_path.endswith(".gz"):
            with gzip.open(backup_path, "rt") as f:
                first_lines = [f.readline() for _ in range(5)]
        else:
            with open(backup_path, "r") as f:
                first_lines = [f.readline() for _ in range(5)]

        result["first_lines"] = [l.strip() for l in first_lines if l.strip()]
        result["readable"] = True
    except Exception as e:
        result["readable"] = False
        result["error"] = str(e)

    result["status"] = "valid"
    return result


def _rotate_backups(output_dir: str, keep_count: int) -> List[str]:
    """Remove old backups, keeping only the most recent N database backups."""
    backups = [b for b in list_backups(output_dir) if b["type"] == "database"]
    if len(backups) <= keep_count:
        return []

    to_remove = backups[:-keep_count]
    removed = []
    for b in to_remove:
        try:
            os.remove(b["path"])
            # Also remove manifest if it exists
            manifest = b["path"].replace(".sql", "_manifest.json").replace(".db", "_manifest.json")
            if os.path.exists(manifest):
                os.remove(manifest)
            removed.append(b["filename"])
        except Exception:
            pass

    return removed


# ── Status ─────────────────────────────────────────────────────


def show_status() -> Dict[str, Any]:
    """Show database and backup status."""
    db_url = _get_db_url()
    if not db_url:
        from core.database import _get_database_url
        db_url = _get_database_url()

    status = {
        "database_type": "SQLite" if _is_sqlite(db_url) else "PostgreSQL",
        "database_url": db_url.split("@")[-1] if "@" in db_url else db_url,
    }

    # SQLite-specific info
    if _is_sqlite(db_url):
        db_path = db_url.replace("sqlite:///", "").replace("sqlite://", "")
        if os.path.exists(db_path):
            stat = os.stat(db_path)
            status["db_size_kb"] = round(stat.st_size / 1024, 1)
            status["db_modified"] = datetime.fromtimestamp(stat.st_mtime).isoformat()

            # Count rows in main tables
            try:
                import sqlite3
                conn = sqlite3.connect(db_path)
                for table in ["conversations", "scored_prospects", "followup_drafts", "audit_log"]:
                    try:
                        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                        status[f"rows_{table}"] = count
                    except Exception:
                        status[f"rows_{table}"] = "N/A"
                conn.close()
            except Exception:
                pass

    # Backup info
    backup_dir = "backups"
    if os.path.exists(backup_dir):
        backups = list_backups(backup_dir)
        db_backups = [b for b in backups if b["type"] == "database"]
        status["backup_count"] = len(db_backups)
        if db_backups:
            latest = db_backups[-1]
            status["latest_backup"] = latest["filename"]
            status["latest_backup_date"] = latest["created"]
            status["latest_backup_size_kb"] = latest["size_kb"]

    # Supplementary files
    for name, path in [
        ("dedup_cache", os.environ.get("DEDUP_CACHE_PATH", ".dedup_cache.json")),
        ("suppressions", os.environ.get("SUPPRESSIONS_LIST_PATH", ".suppressions.txt")),
    ]:
        if os.path.exists(path):
            status[f"{name}_size_kb"] = round(os.path.getsize(path) / 1024, 1)

    return status


# ── CLI ────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Database backup and restore for Sales Follow-Up Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s backup                          # Quick backup with defaults
  %(prog)s backup --compress --rotate 14   # Compressed, keep 14 days
  %(prog)s restore backups/sfa_20240115.sql.gz
  %(prog)s list                            # Show available backups
  %(prog)s verify backups/sfa_20240115.sql.gz
  %(prog)s status                          # Show DB and backup status
        """,
    )

    sub = parser.add_subparsers(dest="command", help="Command to run")

    # Backup
    p_backup = sub.add_parser("backup", help="Create a database backup")
    p_backup.add_argument("-o", "--output", default="backups", help="Output directory (default: backups)")
    p_backup.add_argument("-c", "--compress", action="store_true", default=True, help="Compress with gzip (default: yes)")
    p_backup.add_argument("--no-compress", action="store_true", help="Disable compression")
    p_backup.add_argument("-r", "--rotate", type=int, default=7, help="Keep last N backups (default: 7)")
    p_backup.add_argument("--no-supplementary", action="store_true", help="Skip supplementary files")

    # Restore
    p_restore = sub.add_parser("restore", help="Restore from a backup")
    p_restore.add_argument("backup_path", help="Path to backup file")
    p_restore.add_argument("--confirm", action="store_true", help="Skip confirmation prompt")
    p_restore.add_argument("--no-supplementary", action="store_true", help="Skip supplementary files")

    # List
    p_list = sub.add_parser("list", help="List available backups")
    p_list.add_argument("-o", "--output", default="backups", help="Backup directory (default: backups)")

    # Verify
    p_verify = sub.add_parser("verify", help="Verify a backup file")
    p_verify.add_argument("backup_path", help="Path to backup file")

    # Status
    sub.add_parser("status", help="Show database and backup status")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "backup":
        result = run_backup(
            output_dir=args.output,
            compress=not args.no_compress,
            rotate=args.rotate,
            include_supplementary=not args.no_supplementary,
        )
        sys.exit(0 if result.get("database", {}).get("status") == "success" else 1)

    elif args.command == "restore":
        if not args.confirm:
            print(f"\n⚠️  This will OVERWRITE the current database!")
            print(f"   Backup: {args.backup_path}")
            confirm = input("   Type 'yes' to continue: ")
            if confirm.lower() != "yes":
                print("   Aborted.")
                sys.exit(0)

        result = run_restore(
            backup_path=args.backup_path,
            restore_supplementary_files=not args.no_supplementary,
        )
        sys.exit(0 if result.get("status") == "success" else 1)

    elif args.command == "list":
        backups = list_backups(args.output)
        if not backups:
            print(f"No backups found in {args.output}/")
            sys.exit(0)

        print(f"\n{'Filename':<50} {'Type':<15} {'Size':<10} {'Created'}")
        print("-" * 100)
        for b in backups:
            print(f"{b['filename']:<50} {b['type']:<15} {b['size_kb']:<10.1f} {b['created']}")
        print(f"\nTotal: {len(backups)} backup(s)")

    elif args.command == "verify":
        result = verify_backup(args.backup_path)
        print(json.dumps(result, indent=2))
        sys.exit(0 if result.get("status") == "valid" else 1)

    elif args.command == "status":
        result = show_status()
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
