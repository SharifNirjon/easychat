-- 0002: UI blocks on assistant messages (cards, chips, lists... see api/blocks.py).
--
-- Blocks are validated by the API before they are stored; the CHECK is a last line of defence
-- against oversized or malformed data. Row-level security already covers the column, since it
-- lives on `messages`.

ALTER TABLE messages
  ADD COLUMN blocks jsonb
  CHECK (blocks IS NULL OR (jsonb_typeof(blocks) = 'array' AND pg_column_size(blocks) <= 32000));

-- The API inserts blocks with assistant replies; it already holds INSERT on messages.
