-- Applied to the "gdquiet" Supabase project on 2026-11-28.
-- Legacy account-number functions from an old GD Quiet design (removed from the app).
-- They are SECURITY DEFINER and were callable through the public API; revoke that.
-- Kept (not dropped) so this is easy to undo with GRANT EXECUTE if ever needed.
revoke execute on function public.gdquiet_mod_report(text, double precision, boolean, boolean, text) from public, anon, authenticated;
revoke execute on function public.gdquiet_mod_sync(text) from public, anon, authenticated;
revoke execute on function public.generate_gdquiet_account_number() from public, anon, authenticated;
revoke execute on function public.get_or_create_gdquiet_account() from public, anon, authenticated;
revoke execute on function public.update_gdquiet_mute_percent(double precision) from public, anon, authenticated;
