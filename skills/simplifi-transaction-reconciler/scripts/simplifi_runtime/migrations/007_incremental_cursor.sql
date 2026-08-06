-- Keep API incremental-sync provenance with the run that consumed it.
ALTER TABLE runs ADD COLUMN cursor_before TEXT;
ALTER TABLE runs ADD COLUMN cursor_after TEXT;
