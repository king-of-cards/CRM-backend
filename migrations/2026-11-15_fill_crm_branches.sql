-- One-time data fill for the CRM MySQL database (never run this on the spin PostgreSQL DB).
--
-- crm_branches was empty, so the backend didn't know a spin lead's branch or the branch's vendor
-- (exports showed "Branch 7", and vendor visibility differed from the screen's chips). This adds
-- the 12 stores with the SAME ids as the workflow's spin_branch table (names, codes and slugs
-- from spin_branch; cities from the frontend's store list, stores.js, as spin_branch has none),
-- and each store's vendor exactly as stores.js assigns it. Vendors are looked up by name.
--
-- Applied on 2026-11-15. crm_branches was empty; running this twice fails on the primary key
-- instead of creating duplicates. app/services/stores.py stays as the fallback for branch ids
-- that aren't in crm_branches.

INSERT INTO crm_branches (id, name, code, slug, city, vendor_id, is_active, source, created_at, updated_at) VALUES
 (1,  'Chamrajpet',              'CHA-001',     'chamrajpet',              'Bangalore',     (SELECT id FROM crm_vendors WHERE name = 'RJ'),     1, 'workflow', UTC_TIMESTAMP(), UTC_TIMESTAMP()),
 (2,  'HSR Layout',              'HSR 002',     'hsr-layout',              'Bangalore',     (SELECT id FROM crm_vendors WHERE name = 'Phodeo'), 1, 'workflow', UTC_TIMESTAMP(), UTC_TIMESTAMP()),
 (3,  'Hoodi(whitefield)',       'Hoodi-003',   'hoodi-whitefield',        'Bangalore',     (SELECT id FROM crm_vendors WHERE name = 'Phodeo'), 1, 'workflow', UTC_TIMESTAMP(), UTC_TIMESTAMP()),
 (4,  'Jayanagar',               'JNG-004',     'jayanagar',               'Bangalore',     (SELECT id FROM crm_vendors WHERE name = 'Phodeo'), 1, 'workflow', UTC_TIMESTAMP(), UTC_TIMESTAMP()),
 (5,  'Bommasandra',             'BMSD-005',    'bommasandra',             'Bangalore',     (SELECT id FROM crm_vendors WHERE name = 'Phodeo'), 1, 'workflow', UTC_TIMESTAMP(), UTC_TIMESTAMP()),
 (6,  'Sahakar Nagar',           'SKN-006',     'sahakar-nagar',           'Bangalore',     (SELECT id FROM crm_vendors WHERE name = 'Phodeo'), 1, 'workflow', UTC_TIMESTAMP(), UTC_TIMESTAMP()),
 (7,  'Hyderabad - Kondhapur',   'HYB-007',     'hyderabad',               'Hyderabad',     (SELECT id FROM crm_vendors WHERE name = 'OHP'),    1, 'workflow', UTC_TIMESTAMP(), UTC_TIMESTAMP()),
 (8,  'Visakhapatnam',           'VIZ-008',     'visakhapatnam',           'Visakhapatnam', (SELECT id FROM crm_vendors WHERE name = 'OHP'),    1, 'workflow', UTC_TIMESTAMP(), UTC_TIMESTAMP()),
 (9,  'Hubballi',                'HUBL-009',    'hubballi',                'Hubballi',      (SELECT id FROM crm_vendors WHERE name = 'OHP'),    1, 'workflow', UTC_TIMESTAMP(), UTC_TIMESTAMP()),
 (10, 'Mysore',                  'MYS-010',     'mysore',                  'Mysore',        (SELECT id FROM crm_vendors WHERE name = 'OHP'),    1, 'workflow', UTC_TIMESTAMP(), UTC_TIMESTAMP()),
 (11, 'Chitradurga',             'CTRD-011',    'chitradurga',             'Chitradurga',   (SELECT id FROM crm_vendors WHERE name = 'OHP'),    1, 'workflow', UTC_TIMESTAMP(), UTC_TIMESTAMP()),
 (12, 'Banjara Hills Hyderabad', 'BANJARA-009', 'banjara-hills-hyderabad', 'Hyderabad',     (SELECT id FROM crm_vendors WHERE name = 'OHP'),    1, 'workflow', UTC_TIMESTAMP(), UTC_TIMESTAMP());

-- Check: expect 12 rows, every one with a vendor (none NULL)
SELECT b.id, b.name, b.code, b.city, v.name AS vendor, b.is_active, b.source
  FROM crm_branches b LEFT JOIN crm_vendors v ON v.id = b.vendor_id
 ORDER BY b.id;

-- Rollback (removes exactly these 12 rows, including any later edits to them on the Branches page)
-- DELETE FROM crm_branches WHERE id BETWEEN 1 AND 12 AND source = 'workflow';
