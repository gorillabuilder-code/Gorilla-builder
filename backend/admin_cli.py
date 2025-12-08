"""
admin_cli.py — gor://a tiny admin helper

Usage:
    python -m backend.admin_cli list-projects
"""

from __future__ import annotations
import sys
from supabase import create_client

from .settings import get_settings

settings = get_settings()
supabase = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)


def list_projects():
    res = supabase.table("projects").select("id,name,slug,owner_id").execute()
    for row in res.data or []:
        print(f'{row["id"]} | {row["name"]} | {row["slug"]} | {row["owner_id"]}')


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m backend.admin_cli <command>")
        return

    cmd = sys.argv[1]
    if cmd == "list-projects":
        list_projects()
    else:
        print(f"Unknown command: {cmd}")


if __name__ == "__main__":
    main()
