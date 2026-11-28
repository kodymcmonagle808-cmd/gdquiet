"""Supabase project settings for Discord sign-in.

Left empty in the repository. The release workflow fills these in from the
SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY (or SUPABASE_ANON_KEY) GitHub secrets
before building. The publishable/anon key is designed to ship inside client
apps; access to data is controlled by Row Level Security in Supabase.

For local development, set GDQUIET_SUPABASE_URL and GDQUIET_SUPABASE_KEY
environment variables instead of editing this file.
"""

SUPABASE_URL = ""
SUPABASE_KEY = ""
