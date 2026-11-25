-- 0004: guardrail verdicts on messages (roadmap step 6, see api/guards.py).
--
-- On a visitor message: the injection patterns it matched (e.g. {override,prompt_request}).
-- On an assistant message: the output-guard violation that replaced the reply (e.g. {order_claim});
-- the stored content is then the safe reply, never the blocked text.
-- NULL = nothing flagged. Names only, never message text. RLS already covers the column.

ALTER TABLE messages
  ADD COLUMN guard_flags text[]
  CHECK (guard_flags IS NULL OR (cardinality(guard_flags) BETWEEN 1 AND 10
                                 AND array_to_string(guard_flags, ',') ~ '^[a-z_,]+$'
                                 AND length(array_to_string(guard_flags, ',')) <= 300));
