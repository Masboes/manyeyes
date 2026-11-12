-- The model that actually answered (a CLI alias such as "sonnet" can resolve
-- to different models across CLI versions).
ALTER TABLE reviewer_runs ADD COLUMN model_resolved TEXT;
