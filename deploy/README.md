# Host your own TeamLab website

The local app needs no hosting accounts. A shared website needs your own
Streamlit Community Cloud, Supabase and Cloudflare Turnstile configuration.
Publishing this source alone does not configure those services. The existing
TeamLab website already has them; do not reapply its database setup.

## Set up the services

1. Create a dedicated Supabase project. Apply the four SQL files in
   `deploy/supabase/` in numbered order. Keep the `convolab-private` storage
   bucket private and preserve all supplied ownership and expiry policies.
2. Enable anonymous sign-ins with Cloudflare Turnstile protection. Disable
   unused email signup. Add your exact website hostname to the Turnstile widget.
   Enter its secret directly into Supabase Auth's CAPTCHA settings.
3. Deploy `deploy/functions/private-download/` and `deploy/functions/cleanup/`
   as Supabase Edge Functions. The accompanying `config.toml` disables the
   legacy JWT-only gateway check because these handlers enforce their own
   visitor/session or cleanup-secret checks. Keep those handler checks intact.
4. In private function settings, set `CONVOLAB_SECRET_KEY` to your Supabase
   service key. For private-download also set `CONVOLAB_PUBLISHABLE_KEY` and
   `CONVOLAB_WEBSITE_ORIGIN` to your exact HTTPS website origin. For cleanup,
   set a separate random `CONVOLAB_CLEANUP_SECRET` of at least 32 letters/digits.
   Supabase supplies each function's `SUPABASE_URL`.
5. Deploy `hosted_app.py` with Python 3.12 on Streamlit Community Cloud. Enter
   the values named in `deploy/STAGING_SETTINGS.example.toml` directly into
   Streamlit's private Secrets settings. Keep both enable flags at `"0"`
   while setting up. The internal `CONVOLAB_` names remain intentional.

Never commit real values, copy them into study files or include them in logs.
Public browser identifiers are different from private service credentials.

## Configure independent cleanup

Enable Supabase Cron and the pg_net extension. In Vault, save the cleanup
function's URL as `convolab_cleanup_url` and the same cleanup secret as
`convolab_cleanup_token`. Keep Vault, Cron and net outside exposed Data API
schemas and deny visitor access to their secrets and execution privileges.

After verifying these settings, create one five-minute Cron job with this SQL:

```sql
select net.http_post(
  url := (select decrypted_secret from vault.decrypted_secrets
          where name = 'convolab_cleanup_url'),
  headers := jsonb_build_object(
    'Content-Type', 'application/json',
    'Authorization', 'Bearer ' ||
      (select decrypted_secret from vault.decrypted_secrets
       where name = 'convolab_cleanup_token')),
  body := '{}'::jsonb,
  timeout_milliseconds := 30000
);
```

Use the schedule `*/5 * * * *`. Do not create duplicate jobs or place secret
values directly in the scheduled SQL. Cleanup removes obsolete/expired files
and records, then eligible anonymous identities after seven inactive days once
their data is gone. Access still expires if physical deletion is delayed.
See Supabase's [Cron setup](https://supabase.com/docs/guides/cron) and
[scheduled functions](https://supabase.com/docs/guides/functions/schedule-functions).

## Verify before sharing

Enable signup, verify bot-protected connection, then enable execution for a mock
test. Confirm two browser profiles have separate results, owned ZIP downloads
work, expiry denies access and the cleanup job succeeds. Keep provider keys
temporary. Only then make website access public. Recheck service quotas and
availability before a testing session; do not assume free hosting is always on.

For maintenance, wait for active or paused studies to finish. Set execution
enabled to `"0"` before an update that restarts the app, preserving signup for
downloads. Deploy only checked code and restore execution after verification.
An interrupted execution cannot resume. To withdraw public access, return the
website's Sharing setting to private; published source may already be copied.
