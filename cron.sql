-- Scheduling for aggregation, detection-history folding and retention.
--
-- On GCP these jobs run from a single Cloud Scheduler job calling
-- POST /internal/refresh (see README, M1). This file is for databases where
-- pg_cron is available (e.g. Supabase) and is kept for completeness. pg_cron
-- runs one statement per job, so each step is scheduled separately.

CREATE EXTENSION IF NOT EXISTS pg_cron;

-- Recompute aggregated cells (every 5 minutes).
SELECT cron.schedule('refresh-daily-cells', '*/5 * * * *',
                     'SELECT refresh_daily_cells();');

-- Fold detection-only history (every 15 minutes; runs after refresh).
SELECT cron.schedule('fold-detection-months', '*/15 * * * *',
                     'SELECT fold_detection_months();');

-- Retention: raw reports 72 hours, daily cells 90 days (nightly).
SELECT cron.schedule('prune-reports', '15 3 * * *',
                     'SELECT prune_reports(72);');
SELECT cron.schedule('prune-daily-cells', '20 3 * * *',
                     'SELECT prune_daily_cells(90);');
