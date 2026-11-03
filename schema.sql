-- ============================================================================
-- raymap schema — Postgres + PostGIS
--
-- Privacy model: NO precise coordinates are ever stored. Ingest snaps each
-- report to a geohash cell (precision 6 ~= 1.2 km x 0.6 km) and discards
-- lat/lon, so per-report jitter and its averaging weakness are gone.
--
-- Retention (shortest viable, history preserved):
--   reports            72 hours    raw rows (dedup, retractions, replay)
--   daily_cells        90 days     recent presence, day x cell
--   detection_months   indefinite  only cells with detections, month x cell
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS postgis;

-- Dev note: the schema changed in M0 (lat/lon removed, cell added). Reset the
-- dev volume before re-seeding:  docker compose down -v && docker compose up -d

-- ---------------------------------------------------------------------------
-- Raw reports
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS reports (
    id          bigserial PRIMARY KEY,
    device_hash text NOT NULL,             -- random per-install id, hashed
    ts          timestamptz NOT NULL,
    cell        text NOT NULL CHECK (char_length(cell) = 6),
    rssi_dbm    integer,
    detection   text,
    network     text,
    UNIQUE (device_hash, ts)                -- idempotent re-uploads
);

CREATE INDEX IF NOT EXISTS idx_reports_ts   ON reports (ts);
CREATE INDEX IF NOT EXISTS idx_reports_cell ON reports (cell);

-- ---------------------------------------------------------------------------
-- Aggregated cells: one row per (day, geohash cell)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS daily_cells (
    day                     date NOT NULL,
    cell                    text NOT NULL CHECK (char_length(cell) = 6),
    device_count            integer NOT NULL DEFAULT 0,
    report_count            integer NOT NULL DEFAULT 0,
    detection_count         integer NOT NULL DEFAULT 0,
    detection_device_count  integer NOT NULL DEFAULT 0,  -- distinct devices with >=1 detection
    last_ts                 timestamptz,
    PRIMARY KEY (day, cell)
);

CREATE INDEX IF NOT EXISTS idx_daily_cells_day ON daily_cells (day);

-- (Re)compute aggregates from raw reports (idempotent upsert).
CREATE OR REPLACE FUNCTION refresh_daily_cells(p_lookback_days integer DEFAULT 30)
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    INSERT INTO daily_cells (day, cell, device_count, report_count,
                             detection_count, detection_device_count, last_ts)
    SELECT
        date_trunc('day', ts)::date                                        AS day,
        cell,
        count(DISTINCT device_hash)                                        AS device_count,
        count(*)                                                           AS report_count,
        count(*) FILTER (WHERE detection IS NOT NULL)                      AS detection_count,
        count(DISTINCT device_hash) FILTER (WHERE detection IS NOT NULL)   AS detection_device_count,
        max(ts)                                                            AS last_ts
    FROM reports
    WHERE ts > now() - make_interval(days => p_lookback_days)
    GROUP BY 1, 2
    ON CONFLICT (day, cell) DO UPDATE SET
        device_count           = EXCLUDED.device_count,
        report_count           = EXCLUDED.report_count,
        detection_count        = EXCLUDED.detection_count,
        detection_device_count = EXCLUDED.detection_device_count,
        last_ts                = EXCLUDED.last_ts;
END;
$$;

-- ---------------------------------------------------------------------------
-- Detection history: the searchable public record. Only cells that recorded a
-- detection are folded, so no presence history survives. Counts only move up
-- (GREATEST) so pruning recent detail can never shrink the archive.
-- detection_device_count is the max number of devices reporting a detection
-- in any single day of the month (conservative: never overstates corroboration).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS detection_months (
    month                   date NOT NULL,
    cell                    text NOT NULL CHECK (char_length(cell) = 6),
    detection_count         integer NOT NULL DEFAULT 0,
    detection_device_count  integer NOT NULL DEFAULT 0,
    last_report             timestamptz,
    PRIMARY KEY (month, cell)
);

CREATE INDEX IF NOT EXISTS idx_detection_months_cell ON detection_months (month, cell);

CREATE OR REPLACE FUNCTION fold_detection_months()
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    INSERT INTO detection_months (month, cell, detection_count, detection_device_count, last_report)
    SELECT
        date_trunc('month', day)::date,
        cell,
        sum(detection_count),
        max(detection_device_count),
        max(last_ts)
    FROM daily_cells
    WHERE detection_count > 0
    GROUP BY 1, 2
    ON CONFLICT (month, cell) DO UPDATE SET
        detection_count        = GREATEST(detection_months.detection_count, EXCLUDED.detection_count),
        detection_device_count = GREATEST(detection_months.detection_device_count, EXCLUDED.detection_device_count),
        last_report            = GREATEST(detection_months.last_report, EXCLUDED.last_report);
END;
$$;

-- ---------------------------------------------------------------------------
-- Retention
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION prune_reports(p_retain_hours integer DEFAULT 72)
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    DELETE FROM reports WHERE ts < now() - make_interval(hours => p_retain_hours);
END;
$$;

CREATE OR REPLACE FUNCTION prune_daily_cells(p_retain_days integer DEFAULT 90)
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    DELETE FROM daily_cells WHERE day < (now() - make_interval(days => p_retain_days))::date;
END;
$$;

-- ---------------------------------------------------------------------------
-- Abuse control: devices the operator has banned (checked at ingest).
-- A banned device can mint a new id, but loses its report history and has to
-- pay proof-of-work again.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS blocked_devices (
    device_hash text PRIMARY KEY,
    reason      text,
    blocked_at  timestamptz NOT NULL DEFAULT now()
);
