#!/usr/bin/env bash
set -euo pipefail

psql --username "$POSTGRES_USER" --dbname postgres --set ON_ERROR_STOP=1 \
  --set platform_password="$V7_PLATFORM_DB_PASSWORD" \
  --set data_password="$V7_DATA_DB_PASSWORD" \
  --set execution_password="$V7_EXECUTION_DB_PASSWORD" \
  --set knowledge_password="$V7_KNOWLEDGE_DB_PASSWORD" <<'SQL'
CREATE ROLE v7_platform LOGIN PASSWORD :'platform_password';
CREATE ROLE v7_data LOGIN PASSWORD :'data_password';
CREATE ROLE v7_execution LOGIN PASSWORD :'execution_password';
CREATE ROLE v7_knowledge LOGIN PASSWORD :'knowledge_password';
CREATE DATABASE v7_platform OWNER v7_platform;
CREATE DATABASE v7_data OWNER v7_data;
CREATE DATABASE v7_execution OWNER v7_execution;
CREATE DATABASE v7_knowledge OWNER v7_knowledge;
REVOKE ALL ON DATABASE v7_platform FROM PUBLIC;
REVOKE ALL ON DATABASE v7_data FROM PUBLIC;
REVOKE ALL ON DATABASE v7_execution FROM PUBLIC;
REVOKE ALL ON DATABASE v7_knowledge FROM PUBLIC;
SQL
