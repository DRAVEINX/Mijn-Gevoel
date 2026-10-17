using Microsoft.Data.Sqlite;

namespace MoodPress.CloudPortal;

public sealed class CloudDatabase
{
    private readonly string _connectionString;

    public CloudDatabase(IConfiguration configuration, IHostEnvironment environment)
    {
        var configured = Environment.GetEnvironmentVariable("MOODPRESS_DATABASE_PATH")
            ?? configuration["MoodPress:DatabasePath"];
        var path = string.IsNullOrWhiteSpace(configured)
            ? Path.Combine(environment.ContentRootPath, "data", "moodpress-cloud.db")
            : Path.GetFullPath(configured);
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        _connectionString = new SqliteConnectionStringBuilder
        {
            DataSource = path,
            Mode = SqliteOpenMode.ReadWriteCreate,
            Cache = SqliteCacheMode.Shared,
            ForeignKeys = true,
            Pooling = true,
            DefaultTimeout = 15
        }.ToString();
    }

    public async Task<SqliteConnection> OpenAsync(CancellationToken cancellationToken = default)
    {
        var connection = new SqliteConnection(_connectionString);
        await connection.OpenAsync(cancellationToken);
        await using var command = connection.CreateCommand();
        command.CommandText = "PRAGMA foreign_keys=ON; PRAGMA busy_timeout=15000;";
        await command.ExecuteNonQueryAsync(cancellationToken);
        return connection;
    }

    public async Task InitializeAsync(CancellationToken cancellationToken = default)
    {
        await using var connection = await OpenAsync(cancellationToken);
        await using var command = connection.CreateCommand();
        command.CommandText = """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                password_salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                revoked INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS ix_sessions_user ON sessions(user_id, revoked);
            CREATE TABLE IF NOT EXISTS workspaces (
                id TEXT PRIMARY KEY,
                owner_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_workspaces_owner ON workspaces(owner_user_id);
            CREATE TABLE IF NOT EXISTS memberships (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                role TEXT NOT NULL,
                permissions_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                UNIQUE(workspace_id, user_id)
            );
            CREATE INDEX IF NOT EXISTS ix_memberships_user ON memberships(user_id, active);
            CREATE TABLE IF NOT EXISTS invitations (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                email TEXT NOT NULL,
                role TEXT NOT NULL,
                permissions_json TEXT NOT NULL,
                code_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used_by TEXT REFERENCES users(id) ON DELETE SET NULL,
                revoked INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS ix_invitations_workspace ON invitations(workspace_id, revoked);
            CREATE TABLE IF NOT EXISTS sync_snapshots (
                workspace_id TEXT PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
                version INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                vault_id TEXT NOT NULL DEFAULT '',
                encrypted_archive BLOB NOT NULL,
                updated_at TEXT NOT NULL,
                updated_by TEXT NOT NULL REFERENCES users(id),
                device_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS shared_items (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                category TEXT NOT NULL,
                iv BLOB NOT NULL,
                ciphertext BLOB NOT NULL,
                created_at TEXT NOT NULL,
                created_by TEXT NOT NULL REFERENCES users(id),
                revoked INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS ix_shared_workspace ON shared_items(workspace_id, revoked, category);
            CREATE TABLE IF NOT EXISTS support_suggestions (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                member_id TEXT NOT NULL REFERENCES memberships(id) ON DELETE CASCADE,
                category TEXT NOT NULL,
                iv BLOB NOT NULL,
                ciphertext BLOB NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                decided_at TEXT,
                decided_by TEXT REFERENCES users(id)
            );
            CREATE INDEX IF NOT EXISTS ix_suggestions_workspace ON support_suggestions(workspace_id, status, created_at);
            """;
        await command.ExecuteNonQueryAsync(cancellationToken);

        // Migrate databases created by an earlier preview without dropping their snapshots.
        await using var columns = connection.CreateCommand();
        columns.CommandText = "PRAGMA table_info(sync_snapshots);";
        var hasVaultId = false;
        await using (var reader = await columns.ExecuteReaderAsync(cancellationToken))
        {
            while (await reader.ReadAsync(cancellationToken))
                if (string.Equals(reader.GetString(1), "vault_id", StringComparison.OrdinalIgnoreCase))
                    hasVaultId = true;
        }
        if (!hasVaultId)
        {
            await using var migration = connection.CreateCommand();
            migration.CommandText = "ALTER TABLE sync_snapshots ADD COLUMN vault_id TEXT NOT NULL DEFAULT ''; ";
            await migration.ExecuteNonQueryAsync(cancellationToken);
        }
    }
}
