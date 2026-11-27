-- The API token that sent the job (NULL: a browser session, MCP sign-in, GitHub or CLI).
-- The name is copied: it stays when the token is revoked.
ALTER TABLE jobs ADD COLUMN token_id TEXT;
ALTER TABLE jobs ADD COLUMN token_name TEXT;
