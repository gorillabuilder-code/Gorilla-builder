-- ==========================================================
-- gor://a — INITIAL DATABASE MIGRATION
-- Purpose:
--   Full persistent storage for multi-project AI app building
--   Durable, safe, atomic file management
--   AI snapshot + rollback infrastructure
-- ==========================================================

-- Enable required extensions
create extension if not exists "uuid-ossp";
create extension if not exists "pgcrypto";
create extension if not exists "vector";

-- ==========================================================
-- USERS — Supabase manages users table internally
-- ==========================================================

-- ==========================================================
-- PROJECTS
-- Each app the user builds is a project
-- ==========================================================
create table if not exists projects (
    id uuid primary key default uuid_generate_v4(),
    owner_id uuid references auth.users(id) on delete cascade,
    name text not null,
    description text,
    created_at timestamptz default now(),
    updated_at timestamptz default now()
);

create index if not exists idx_projects_owner on projects(owner_id);


-- ==========================================================
-- FILES
-- Virtual file system per project (frontend/backend/sql/etc)
-- Durable + versioned + atomic writes applied here
-- ==========================================================
create table if not exists files (
    id uuid primary key default uuid_generate_v4(),
    project_id uuid references projects(id) on delete cascade,
    path text not null,
    content text default '',
    hash bytea,
    updated_at timestamptz default now(),
    created_at timestamptz default now()
);

create unique index if not exists idx_file_unique_path on files(project_id, path);
create index if not exists idx_files_project on files(project_id);


-- ==========================================================
-- SNAPSHOTS
-- AI-generated or user-triggered versioning restores
-- Stores compressed encoded snapshot for rollback
-- ==========================================================
create table if not exists snapshots (
    id uuid primary key default uuid_generate_v4(),
    project_id uuid references projects(id) on delete cascade,
    label text,
    data bytea not null, -- compressed snapshot archive
    created_at timestamptz default now()
);

create index if not exists idx_snapshots_project on snapshots(project_id);


-- ==========================================================
-- WAL — Write Ahead Log (AI agent operations)
-- Stores atomic reversible changes before applied
-- ==========================================================
create table if not exists wal (
    id uuid primary key default uuid_generate_v4(),
    project_id uuid references projects(id) on delete cascade,
    operation jsonb not null,
    applied boolean default false,
    created_at timestamptz default now()
);

create index if not exists idx_wal_project on wal(project_id);


-- ==========================================================
-- VECTOR EMBEDDINGS (for future AI search & context awareness)
-- ==========================================================
create table if not exists file_embeddings (
    file_id uuid references files(id) on delete cascade,
    embedding vector(1536), -- future-proof: supports major providers
    updated_at timestamptz default now()
);

create index if not exists idx_embeddings_file on file_embeddings(file_id);
create index if not exists idx_embeddings_vector on file_embeddings using ivfflat (embedding vector_cosine_ops);

