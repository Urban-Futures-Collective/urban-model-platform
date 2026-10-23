-- =============================================================================
-- Development seed for the model registry (migration 0003, schema `registry`).
--
-- Registers the bundled mock OGC server (scripts/mock_ogc_server.py) with its
-- four processes as published models, so UMP_PROVIDERS_SOURCE=db works locally
-- without onboarding through the registry API. NOT for production: the review
-- data below is made up.
--
--   poetry run ump-migrate upgrade head
--   psql "postgresql://ump:ump@localhost:5433/ump" -f scripts/seed_registry_dev.sql
--   PYTHONPATH=src poetry run uvicorn scripts.mock_ogc_server:app --port 5001
--   UMP_PROVIDERS_SOURCE=db poetry run ump
--
-- Idempotent: re-running it changes nothing. Running the mock server in
-- docker-compose-dev instead? Change base_url to http://mock-ogc-server:5001/.
--
-- Servers with credentials cannot be seeded in plain SQL: `credentials` must
-- be encrypted with UMP_REGISTRY_CREDENTIALS_KEY (FernetCredentialCipher).
-- =============================================================================

BEGIN;

-- Accounts. A published model needs a verifier other than its owner (four-eyes
-- CHECK); these subs only have to match Keycloak users if you log in as them.
INSERT INTO registry.app_user (sub, display_name, email) VALUES
    ('dev-owner',    'Dev Owner',    'owner@example.org'),
    ('dev-verifier', 'Dev Verifier', 'verifier@example.org')
ON CONFLICT (sub) DO NOTHING;

INSERT INTO registry.provider_server (id, name, base_url, owner_sub, auth_type)
VALUES ('00000000-0000-4000-8000-000000000001', 'mock', 'http://localhost:5001/',
        'dev-owner', 'NoAuth')
ON CONFLICT (name) DO NOTHING;

INSERT INTO registry.model (
    slug, provider_server_id, remote_process_id, owner_sub,
    full_name, short_description, purpose, license, process_config,
    status, status_changed_by, status_changed_at, published_at,
    verified_by, verified_at, verified_interface_sha256
)
SELECT 'mock-' || p.id, ps.id, p.id, 'dev-owner',
       jsonb_build_object('de', p.title_de, 'en', p.title_en),
       jsonb_build_object('en', p.summary),
       jsonb_build_object('de', 'Testprozess des Mock-Servers', 'en', 'Mock server test process'),
       'MIT',
       '{"anonymous-access": true}'::jsonb,
       'published', 'dev-verifier', now(), now(),
       -- placeholder hash: the interface was never really verified
       'dev-verifier', now(), repeat('0', 64)
  FROM registry.provider_server ps
 CROSS JOIN (VALUES
       ('echo',        'Echo',               'Echo',        'Returns its inputs'),
       ('hello-world', 'Hallo Welt',         'Hello World', 'Greets the caller'),
       ('slow',        'Langsamer Prozess',  'Slow',        'Finishes after a delay'),
       ('failing-job', 'Fehlschlagender Job','Failing job', 'Always fails')
     ) AS p(id, title_de, title_en, summary)
 WHERE ps.name = 'mock'
ON CONFLICT (provider_server_id, remote_process_id) DO NOTHING;

COMMIT;
