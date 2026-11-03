-- ============================================================================
-- raymap schema — Postgres + PostGIS + H3 (res 3..8)
--
-- Privacy model: NO precise coordinates are ever stored. Ingest snaps each
-- report to H3 cells at resolutions 3..8 and discards lat/lon.
--   r3   12,393 km2  ~138 km across   (national view)
--   r4    1,770 km2  ~52 km
--   r5      253 km2  ~20 km
--   r6       36 km2  ~7.4 km
--   r7      5.2 km2  ~2.8 km
--   r8     0.74 km2  ~1.06 km          (finest allowed; ~ today's geohash-6)
--
-- Retention: raw 72h, daily cells 90d, detection months indefinite.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS postgis;

-- Dev note: M2 changed the key from geohash to H3. Existing installs run
-- scripts/migrate_to_hex.py (presence history is rebuilt from raw reports; the
-- detection archive is re-keyed and preserved).

-- ---------------------------------------------------------------------------
-- Raw reports
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS reports (
    id          bigserial PRIMARY KEY,
    device_hash text NOT NULL,
    ts          timestamptz NOT NULL,
    h3_r3       text NOT NULL CHECK (char_length(h3_r3) = 15),
    h3_r4       text NOT NULL CHECK (char_length(h3_r4) = 15),
    h3_r5       text NOT NULL CHECK (char_length(h3_r5) = 15),
    h3_r6       text NOT NULL CHECK (char_length(h3_r6) = 15),
    h3_r7       text NOT NULL CHECK (char_length(h3_r7) = 15),
    h3_r8       text NOT NULL CHECK (char_length(h3_r8) = 15),
    rssi_dbm    integer,
    detection   text,
    network     text,
    UNIQUE (device_hash, ts)
);

CREATE INDEX IF NOT EXISTS idx_reports_ts ON reports (ts);

-- ---------------------------------------------------------------------------
-- Aggregated cells: one row per (day, resolution, H3 cell)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS daily_cells (
    day                     date NOT NULL,
    res                     smallint NOT NULL CHECK (res BETWEEN 3 AND 8),
    cell                    text NOT NULL CHECK (char_length(cell) = 15),
    device_count            integer NOT NULL DEFAULT 0,
    report_count            integer NOT NULL DEFAULT 0,
    detection_count         integer NOT NULL DEFAULT 0,
    detection_device_count  integer NOT NULL DEFAULT 0,
    last_ts                 timestamptz,
    PRIMARY KEY (day, res, cell)
);

CREATE INDEX IF NOT EXISTS idx_daily_cells_lookup ON daily_cells (res, day);

-- (Re)compute every resolution in one pass (LATERAL unpivots the six columns).
CREATE OR REPLACE FUNCTION refresh_daily_cells(p_lookback_days integer DEFAULT 30)
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    INSERT INTO daily_cells (day, res, cell, device_count, report_count,
                             detection_count, detection_device_count, last_ts)
    SELECT
        date_trunc('day', ts)::date,
        r.res,
        r.cell,
        count(DISTINCT device_hash),
        count(*),
        count(*) FILTER (WHERE detection IS NOT NULL),
        count(DISTINCT device_hash) FILTER (WHERE detection IS NOT NULL),
        max(ts)
    FROM reports,
         LATERAL (VALUES (3, h3_r3), (4, h3_r4), (5, h3_r5),
                         (6, h3_r6), (7, h3_r7), (8, h3_r8)) AS r(res, cell)
    WHERE ts > now() - make_interval(days => p_lookback_days)
    GROUP BY 1, 2, 3
    ON CONFLICT (day, res, cell) DO UPDATE SET
        device_count           = EXCLUDED.device_count,
        report_count           = EXCLUDED.report_count,
        detection_count        = EXCLUDED.detection_count,
        detection_device_count = EXCLUDED.detection_device_count,
        last_ts                = EXCLUDED.last_ts;
END;
$$;

-- ---------------------------------------------------------------------------
-- Detection history: the searchable public record, keyed on H3 r7 (~2.8 km).
-- Only cells with detections are folded, so no presence history survives.
-- Counts only move up (GREATEST) so pruning recent detail cannot shrink it.
-- detection_device_count is the max devices reporting in any single day.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS detection_months (
    month                   date NOT NULL,
    cell                    text NOT NULL CHECK (char_length(cell) = 15),
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
    WHERE res = 7 AND detection_count > 0
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
-- Abuse control
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS blocked_devices (
    device_hash text PRIMARY KEY,
    reason      text,
    blocked_at  timestamptz NOT NULL DEFAULT now()
);
