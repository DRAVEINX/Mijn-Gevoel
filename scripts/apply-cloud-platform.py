#!/usr/bin/env python3
"""Apply the checked-in MoodPress cloud-sync client to the separate iOS or Windows source package.

The release source archives are never modified in-place in the repository. CI extracts a
fresh copy, applies this deterministic port script, then compiles/tests that copy.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path
import shutil
import sys

MAX_RECORD_BYTES = 64 * 1024 * 1024

def locate_source(source_root: Path, target: str) -> tuple[Path, Path]:
    candidates = [source_root]
    if target == "ios":
        candidates.insert(0, source_root / "MijnGevoel-Android")
    else:
        candidates.insert(0, source_root / "MoodPress-Windows-source-2.3.0")
    for root in candidates:
        app_dir = root / ("MijnGevoel.Android" if target == "ios" else "MoodPress.WindowsApp")
        core_dir = root / "MijnGevoel.Core"
        if app_dir.is_dir() and core_dir.is_dir():
            return app_dir, core_dir
    raise FileNotFoundError(
        f"Could not locate the {target} app and Core project under {source_root}. "
        "Check the extracted source archive layout."
    )

def add_vault_validator(vault_file: Path) -> None:
    source = vault_file.read_text(encoding="utf-8")
    if "IsEncryptedRecordForCurrentKey(" in source:
        return
    marker = "    public T? Load<T>(string kind, Guid id)"
    if source.count(marker) != 1:
        raise RuntimeError(f"Expected one VaultStore.Load marker in {vault_file}")
    method = f'''    /// <summary>Validates an encrypted record against this account's active vault key without returning plaintext.</summary>
    public bool IsEncryptedRecordForCurrentKey(string kind, Guid id, byte[]? blob)
    {{
        if (string.IsNullOrWhiteSpace(kind) || kind.Length > 80 ||
            kind.Any(ch => !((ch >= 'a' && ch <= 'z') || (ch >= '0' && ch <= '9') || ch == '-')))
            return false;
        if (blob is null || blob.Length <= 0 || blob.LongLength > {MAX_RECORD_BYTES}L)
            return false;

        byte[]? plain = null;
        try
        {{
            plain = Crypto.Open(_key, blob, $"{kind}:{{id:N}}");
            using var document = JsonDocument.Parse(plain);
            return true;
        }}
        catch (Exception ex) when (ex is CryptographicException or JsonException or ArgumentException or InvalidDataException)
        {{
            return false;
        }}
        finally
        {{
            if (plain is not null) CryptographicOperations.ZeroMemory(plain);
        }}
    }}

'''
    vault_file.write_text(source.replace(marker, method + marker), encoding="utf-8")

def patch_app_session(app_dir: Path) -> None:
    path = app_dir / "AppSession.cs"
    source = path.read_text(encoding="utf-8")
    if "CloudSyncClient.TryAutoSyncAsync()" in source:
        return
    marker = re.compile(r"(public static void Touch\(\)\s*\{\s*LastActivityUtc = DateTime\.UtcNow;)")
    source, count = marker.subn(
        r"\1\n        if (Store is not null) _ = CloudSyncClient.TryAutoSyncAsync();",
        source,
        count=1,
    )
    if count != 1:
        raise RuntimeError(f"Could not add cloud auto-sync hook to {path}")
    path.write_text(source, encoding="utf-8")

def patch_dashboard(app_dir: Path) -> None:
    path = app_dir / "DashboardPage.cs"
    source = path.read_text(encoding="utf-8")
    if "FeatureKind.CloudSync" not in source:
        enum_match = re.search(r"(public enum FeatureKind\s*\{)(.*?)(\})", source, re.S)
        if not enum_match:
            raise RuntimeError(f"Could not find FeatureKind enum in {path}")
        body = enum_match.group(2).rstrip()
        if body and not body.endswith(","):
            body += ","
        body += "\n    CloudSync\n"
        source = source[:enum_match.start(2)] + body + source[enum_match.end(2):]

    help_entry = '("Hulp", "❓", FeatureKind.Help)'
    cloud_entry = '("Cloudsync tussen apparaten", "☁️", FeatureKind.CloudSync)'
    if cloud_entry not in source:
        if source.count(help_entry) != 1:
            raise RuntimeError(f"Could not identify the final dashboard group entry in {path}")
        source = source.replace(help_entry, help_entry + ", " + cloud_entry, 1)

    ordinary_route = "else _ = Navigation.PushAsync(new FeaturePage(kind));"
    cloud_route = "else if (kind == FeatureKind.CloudSync) _ = Navigation.PushAsync(new CloudSyncPage());"
    if cloud_route not in source:
        if source.count(ordinary_route) != 1:
            raise RuntimeError(f"Could not identify the default feature route in {path}")
        source = source.replace(ordinary_route, cloud_route + "\n        " + ordinary_route, 1)
    path.write_text(source, encoding="utf-8")

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=("ios", "windows"), required=True)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--repo-root", required=True, type=Path)
    args = parser.parse_args()

    app_dir, core_dir = locate_source(args.source_root.resolve(), args.target)
    namespace = "MijnGevoel.AndroidApp" if args.target == "ios" else "MoodPress.WindowsApp"
    shared = args.repo_root.resolve() / "platform-port" / "shared"
    for name in ("CloudSyncClient.cs", "CloudArchiveMerger.cs", "CloudSyncPage.cs"):
        source_path = shared / name
        if not source_path.is_file():
            raise FileNotFoundError(f"Shared cloud-sync source is missing: {source_path}")
        source = source_path.read_text(encoding="utf-8")
        if "__MOODPRESS_NAMESPACE__" not in source:
            raise RuntimeError(f"Namespace placeholder is missing from {source_path}")
        destination = app_dir / name
        destination.write_text(source.replace("__MOODPRESS_NAMESPACE__", namespace), encoding="utf-8")

    add_vault_validator(core_dir / "Data" / "VaultStore.cs")
    patch_app_session(app_dir)
    patch_dashboard(app_dir)

    required = [
        app_dir / "CloudSyncClient.cs",
        app_dir / "CloudArchiveMerger.cs",
        app_dir / "CloudSyncPage.cs",
        core_dir / "Data" / "VaultStore.cs",
        app_dir / "AppSession.cs",
        app_dir / "DashboardPage.cs",
    ]
    for path in required:
        if not path.is_file():
            raise FileNotFoundError(path)
    print(f"Applied MoodPress cloud-sync/portal client to {args.target}: {app_dir}")
    return 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Cloud platform patch failed: {exc}", file=sys.stderr)
        raise
