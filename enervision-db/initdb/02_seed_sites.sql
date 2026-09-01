-- ============================================================
-- Seed du référentiel site — 7 sites (SITE001–SITE007)
-- Les 3 premiers viennent de la doc de l'API mock ; les
-- capacités des sites 4 à 7 sont des PLACEHOLDERS à remplacer
-- par les valeurs réelles de GET /api/v1/sites au premier
-- démarrage de l'ETL (UPSERT ci-dessous idempotent).
-- ============================================================

INSERT INTO site (site_id, site_type, site_name, location, capacity_kw, status) VALUES
    ('SITE001', 'office',     'Bureau Paris La Défense', 'Paris, France',     200, 'active'),
    ('SITE002', 'factory',    'Usine Lyon Vénissieux',   'Lyon, France',     1000, 'active'),
    ('SITE003', 'datacenter', 'Data Center Marseille',   'Marseille, France', 800, 'active'),
    ('SITE004', 'unknown',    'Site 4 (à synchroniser)', NULL,                500, 'active'),
    ('SITE005', 'unknown',    'Site 5 (à synchroniser)', NULL,                500, 'active'),
    ('SITE006', 'unknown',    'Site 6 (à synchroniser)', NULL,                500, 'active'),
    ('SITE007', 'unknown',    'Site 7 (à synchroniser)', NULL,                630, 'active')
ON CONFLICT (site_id) DO UPDATE SET
    site_type   = EXCLUDED.site_type,
    site_name   = EXCLUDED.site_name,
    location    = EXCLUDED.location,
    capacity_kw = EXCLUDED.capacity_kw,
    status      = EXCLUDED.status;
