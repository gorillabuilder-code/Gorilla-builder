-- ==========================================================
-- gor://a — PRODUCTION DATABASE MIGRATION (Supabase Native)
-- ==========================================================

-- Required extensions
create extension if not exists "uuid-ossp";
create extension if not exists "pgcrypto";
create extension if not exists "vector";

-- ==========================================================
-- PROJECTS
-- ==========================================================

create table if not exists projects (
    id uuid primary key default uuid_generate_v4(),
    owner_id uuid not null references auth.users(id) on delete cascade,
    name text not null,
    description text,
    created_at timestamptz default now(),
    updated_at timestamptz default now()
);

create index if not exists idx_projects_owner on projects(owner_id);

alter table projects enable row level security;

-- Only owner can access their projects
create policy "projects_owner_access"
on projects
for all
using (auth.uid() = owner_id)
with check (auth.uid() = owner_id);

-- ==========================================================
-- FILES (Virtual File System)
-- ==========================================================

create table if not exists files (
    id uuid primary key default uuid_generate_v4(),
    project_id uuid not null references projects(id) on delete cascade,
    path text not null,
    content text default '',
    hash bytea,
    created_at timestamptz default now(),
    updated_at timestamptz default now()
);

create unique index if not exists idx_file_unique_path
on files(project_id, path);

create index if not exists idx_files_project
on files(project_id);

alter table files enable row level security;

-- Files accessible only if user owns the project
create policy "files_project_owner_access"
on files
for all
using (
    exists (
        select 1
        from projects
        where projects.id = files.project_id
        and projects.owner_id = auth.uid()
    )
)
with check (
    exists (
        select 1
        from projects
        where projects.id = files.project_id
        and projects.owner_id = auth.uid()
    )
);

-- ==========================================================
-- SNAPSHOTS (Rollback / Restore)
-- ==========================================================

create table if not exists snapshots (
    id uuid primary key default uuid_generate_v4(),
    project_id uuid not null references projects(id) on delete cascade,
    label text,
    data bytea not null,
    created_at timestamptz default now()
);

create index if not exists idx_snapshots_project
on snapshots(project_id);

alter table snapshots enable row level security;

create policy "snapshots_project_owner_access"
on snapshots
for all
using (
    exists (
        select 1
        from projects
        where projects.id = snapshots.project_id
        and projects.owner_id = auth.uid()
    )
)
with check (
    exists (
        select 1
        from projects
        where projects.id = snapshots.project_id
        and projects.owner_id = auth.uid()
    )
);

-- ==========================================================
-- WAL (Agent Atomic Operations)
-- ==========================================================

create table if not exists wal (
    id uuid primary key default uuid_generate_v4(),
    project_id uuid not null references projects(id) on delete cascade,
    operation jsonb not null,
    applied boolean default false,
    created_at timestamptz default now()
);

create index if not exists idx_wal_project
on wal(project_id);

alter table wal enable row level security;

create policy "wal_project_owner_access"
on wal
for all
using (
    exists (
        select 1
        from projects
        where projects.id = wal.project_id
        and projects.owner_id = auth.uid()
    )
)
with check (
    exists (
        select 1
        from projects
        where projects.id = wal.project_id
        and projects.owner_id = auth.uid()
    )
);

-- ==========================================================
-- FILE EMBEDDINGS (Future AI Context)
-- ==========================================================

create table if not exists file_embeddings (
    file_id uuid primary key references files(id) on delete cascade,
    embedding vector(1536),
    updated_at timestamptz default now()
);

create index if not exists idx_embeddings_file
on file_embeddings(file_id);

create index if not exists idx_embeddings_vector
on file_embeddings
using ivfflat (embedding vector_cosine_ops);

alter table file_embeddings enable row level security;

create policy "embeddings_file_owner_access"
on file_embeddings
for all
using (
    exists (
        select 1
        from files
        join projects on projects.id = files.project_id
        where files.id = file_embeddings.file_id
        and projects.owner_id = auth.uid()
    )
)
with check (
    exists (
        select 1
        from files
        join projects on projects.id = files.project_id
        where files.id = file_embeddings.file_id
        and projects.owner_id = auth.uid()
    )
);
