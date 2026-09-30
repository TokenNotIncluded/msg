-- Review and run once as the installation database owner, in the chosen DB.
-- This file neither creates a database nor changes existing PUBLIC grants.
-- A password/peer map belongs in the operator's secret setup, never this file.
CREATE ROLE msgd_hosting LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOINHERIT NOREPLICATION NOBYPASSRLS;
SELECT format('GRANT CONNECT ON DATABASE %I TO msgd_hosting', current_database()) \gexec
GRANT USAGE ON SCHEMA public TO msgd_hosting;
GRANT SELECT ON public.schema_version, public.resources, public.revisions,
    public.identities, public.memberships, public.credentials, public.certificates, public.oauth_states,
    public.settings, public.share_grants, public.share_grants_v2,
    public.dm_conversations, public.system_sources, public.resource_path_aliases TO msgd_hosting;
ALTER ROLE msgd_hosting SET default_transaction_read_only = on;
ALTER ROLE msgd_hosting SET search_path = pg_catalog, public, pg_temp;
-- No role membership, ownership, CREATE, sequence use, other-table SELECT,
-- column/table writes or callable application SECURITY DEFINER functions.
-- If PUBLIC already grants those, startup refuses; review that DB separately.
