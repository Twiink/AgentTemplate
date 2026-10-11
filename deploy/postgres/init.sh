#!/usr/bin/env bash
# 首次建立非特权运行角色；密码由 Compose 私有环境注入，不写入版本库。
set -euo pipefail
psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set=app_password="$POSTGRES_PASSWORD" <<'SQL'
CREATE ROLE agent_app LOGIN PASSWORD :'app_password' NOSUPERUSER NOBYPASSRLS;
GRANT CONNECT ON DATABASE agenttemplate TO agent_app;
GRANT USAGE ON SCHEMA public TO agent_app;
ALTER DEFAULT PRIVILEGES FOR ROLE agent_owner IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO agent_app;
ALTER DEFAULT PRIVILEGES FOR ROLE agent_owner IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO agent_app;
SQL
