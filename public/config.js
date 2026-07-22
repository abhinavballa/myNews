// Public Supabase config. The anon key is safe to ship in the client — RLS is
// what protects data, and every table policy restricts rows to auth.uid().
// Fill these in from Supabase → Project Settings → API.
window.MYNEWS_CONFIG = {
  SUPABASE_URL: "https://oiimpprnpwhrigqmbakn.supabase.co",
  SUPABASE_ANON_KEY: "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im9paW1wcHJucHdocmlncW1iYWtuIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODQ2NjQ2NDcsImV4cCI6MjEwMDI0MDY0N30.5qWwgaz1H1JrZMwN61lhhhKAiMdb1SAc9mWEm1f5nvw",
};
