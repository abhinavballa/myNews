// Public Supabase config. The anon key is safe to ship in the client — RLS is
// what protects data, and every table policy restricts rows to auth.uid().
// Fill these in from Supabase → Project Settings → API.
window.MYNEWS_CONFIG = {
  SUPABASE_URL: "https://YOUR-PROJECT-REF.supabase.co",
  SUPABASE_ANON_KEY: "YOUR-ANON-KEY",
};
