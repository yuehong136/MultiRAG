-- MultiRAG PostgreSQL 初始化脚本
-- 创建应用所需的 schema

-- 创建 usr_ai schema（如果不存在）
CREATE SCHEMA IF NOT EXISTS usr_ai;

-- 授予权限
GRANT ALL ON SCHEMA usr_ai TO usr_ai;
