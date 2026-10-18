-- Ruolo esclusivo del simulatore locale: nessun privilegio di scrittura per l'API.
CREATE ROLE clinical_reader LOGIN PASSWORD 'lettura_sintetica';
GRANT CONNECT ON DATABASE clinico TO clinical_reader;
GRANT USAGE ON SCHEMA public TO clinical_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE clinico IN SCHEMA public
    GRANT SELECT ON TABLES TO clinical_reader;
