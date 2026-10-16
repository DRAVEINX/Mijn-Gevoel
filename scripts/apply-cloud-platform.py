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
    fields_marker = "readonly byte[] _key; readonly string _dir;"
    if "CloudSnapshotMagic" not in source:
        if source.count(fields_marker) != 1:
            raise RuntimeError(f"Expected one VaultStore fields marker in {vault_file}")
        fields = """
    const int CloudSnapshotHeaderLength = 36;
    const int MaxCloudSnapshotBytes = 100 * 1024 * 1024;
    static readonly byte[] CloudSnapshotMagic = "MPCLOUD1"u8.ToArray();
    static readonly byte[] CloudSnapshotAad = "MoodPress cloud snapshot v1"u8.ToArray();"""
        source = source.replace(fields_marker, fields_marker + "\n" + fields, 1)

    marker = "    public T? Load<T>(string kind, Guid id)"
    if source.count(marker) != 1:
        raise RuntimeError(f"Expected one VaultStore.Load marker in {vault_file}")

    methods = {
        "GetStableCloudVaultId": """
    /// <summary>Returns a stable, non-reversible identifier for this active high-entropy vault key.</summary>
    public string GetStableCloudVaultId()
    {
        using var hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        hash.AppendData("MoodPress cloud vault identity v1"u8);
        hash.AppendData(_key);
        var digest = hash.GetHashAndReset();
        try { return Convert.ToHexString(digest); }
        finally { CryptographicOperations.ZeroMemory(digest); }
    }

""",
        "EncryptCloudSnapshot": """
    /// <summary>Encrypts an entire backup archive with the active vault key before it leaves this device.</summary>
    public byte[] EncryptCloudSnapshot(byte[] plaintext)
    {
        ArgumentNullException.ThrowIfNull(plaintext);
        if (plaintext.Length == 0 || plaintext.LongLength > MaxCloudSnapshotBytes - CloudSnapshotHeaderLength)
            throw new ArgumentException("De cloudback-up is leeg of te groot.", nameof(plaintext));
        var output = new byte[CloudSnapshotHeaderLength + plaintext.Length];
        var nonce = RandomNumberGenerator.GetBytes(12);
        try
        {
            CloudSnapshotMagic.CopyTo(output, 0);
            nonce.CopyTo(output, 8);
            using var aes = new AesGcm(_key, 16);
            aes.Encrypt(nonce, plaintext, output.AsSpan(CloudSnapshotHeaderLength), output.AsSpan(20, 16), CloudSnapshotAad);
            return output;
        }
        catch
        {
            CryptographicOperations.ZeroMemory(output);
            throw;
        }
        finally { CryptographicOperations.ZeroMemory(nonce); }
    }

""",
        "DecryptCloudSnapshot": """
    /// <summary>Authenticates and decrypts only a snapshot sealed with the active vault key.</summary>
    public byte[] DecryptCloudSnapshot(byte[] envelope)
    {
        ArgumentNullException.ThrowIfNull(envelope);
        if (envelope.Length <= CloudSnapshotHeaderLength || envelope.Length > MaxCloudSnapshotBytes ||
            !envelope.AsSpan(0, 8).SequenceEqual(CloudSnapshotMagic))
            throw new InvalidDataException("De versleutelde cloudback-up is ongeldig of te groot.");
        var plaintext = new byte[envelope.Length - CloudSnapshotHeaderLength];
        try
        {
            using var aes = new AesGcm(_key, 16);
            aes.Decrypt(envelope.AsSpan(8, 12), envelope.AsSpan(CloudSnapshotHeaderLength),
                envelope.AsSpan(20, 16), plaintext, CloudSnapshotAad);
            return plaintext;
        }
        catch
        {
            CryptographicOperations.ZeroMemory(plaintext);
            throw;
        }
    }

""",
        "IsEncryptedRecordForCurrentKey": """
    /// <summary>Validates an encrypted record against this account's active vault key without returning plaintext.</summary>
    public bool IsEncryptedRecordForCurrentKey(string kind, Guid id, byte[]? blob)
    {
        if (string.IsNullOrWhiteSpace(kind) || kind.Length > 80 ||
            kind.Any(ch => !((ch >= 'a' && ch <= 'z') || (ch >= '0' && ch <= '9') || ch == '-')))
            return false;
        if (blob is null || blob.Length <= 0 || blob.LongLength > 67108864L)
            return false;

        byte[]? plain = null;
        try
        {
            plain = Crypto.Open(_key, blob, $"{kind}:{id:N}");
            using var document = JsonDocument.Parse(plain);
            return true;
        }
        catch (Exception ex) when (ex is CryptographicException or JsonException or ArgumentException or InvalidDataException)
        {
            return false;
        }
        finally
        {
            if (plain is not null) CryptographicOperations.ZeroMemory(plain);
        }
    }

"""
    }
    for method_name, method_body in methods.items():
        if f"{method_name}(" not in source:
            source = source.replace(marker, method_body + marker, 1)
    vault_file.write_text(source, encoding="utf-8")

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
    # Fix the old QRCoder System.Drawing.Common 6 dependency conflicting with MAUI .NET 10.
    core_project = core_dir / "MijnGevoel.Core.csproj"
    core_project_text = core_project.read_text(encoding="utf-8")
    qrcoder_pattern = re.compile(r'(<PackageReference\s+Include="QRCoder"\s+Version=")[^"]+("\s*/>)')
    core_project_text, qrcoder_count = qrcoder_pattern.subn(r'\g<1>1.8.1\g<2>', core_project_text, count=1)
    if qrcoder_count != 1:
        raise RuntimeError(f"Could not update QRCoder dependency in {core_project}")
    core_project.write_text(core_project_text, encoding="utf-8")
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
