# Deploying to PythonAnywhere

Roughly fifteen minutes end to end. Replace `yourname` with your PythonAnywhere
username everywhere below.

This guide installs the dependencies directly into your account with
`pip --user` — no virtualenv.

---

## 1. Get the code onto the server

Open a **Bash console** on the PythonAnywhere *Consoles* tab:

```bash
cd ~
git clone <your-repo-url> hair-saloon      # or upload a zip and unzip it
cd hair-saloon
```

## 2. Dependencies

```bash
pip3.11 install --user -r requirements.txt
```

Three things to get right here:

- **`--user` is required.** The system Python on PythonAnywhere is read-only, so
  a plain `pip install` fails. `--user` puts everything in
  `~/.local/lib/python3.11/site-packages`, which is on the path automatically.
- **Use the versioned command** (`pip3.11`, not `pip`) and pick the *same*
  version you select on the Web tab in step 5. Installing with `pip3.11` and
  then running the web app on Python 3.10 is the classic way to get
  `ModuleNotFoundError: No module named 'django'` on a site that works fine in
  the console.
- **PythonAnywhere preinstalls its own Django**, usually an older one. Your
  `--user` install takes precedence over it, so this is fine — but if you ever
  see the wrong version, check with `python3.11 -m django --version` rather than
  trusting the console's `django-admin`.

Everything below uses `python3.11` explicitly for the same reason.

## 3. Settings

```bash
cp .env.example .env
python3.11 -c "from django.core.management.utils import get_random_secret_key as k; print(k())"
nano .env
```

The values that matter on the server:

```ini
DJANGO_SECRET_KEY=<the key you just generated>
DJANGO_DEBUG=False
DJANGO_ALLOWED_HOSTS=yourname.pythonanywhere.com
DJANGO_CSRF_TRUSTED_ORIGINS=https://yourname.pythonanywhere.com
SITE_BASE_URL=https://yourname.pythonanywhere.com
DJANGO_TIME_ZONE=Europe/Berlin
```

`SITE_BASE_URL` is what the accept / decline links in emails point at. If it is
wrong, those links break — it is the single most common deployment mistake here.

## 4. Database and first data

```bash
python3.11 manage.py migrate
python3.11 manage.py init_salon --name "Your Salon" --timezone Europe/Berlin
python3.11 manage.py createsuperuser
python3.11 manage.py collectstatic --noinput
```

SQLite is fine for one salon. To use PythonAnywhere's MySQL instead, create the
database on the *Databases* tab, run
`pip3.11 install --user mysqlclient`, fill in the `DB_*` lines in `.env`, and
re-run `migrate`.

## 5. The Web tab

*Web → Add a new web app → **Manual configuration** → Python 3.11.*

Pick the **same version you installed with in step 2**.

| Field | Value |
| --- | --- |
| Source code | `/home/yourname/hair-saloon` |
| Working directory | `/home/yourname/hair-saloon` |
| Virtualenv | *leave empty* |

Leaving the Virtualenv box blank is what makes the web app fall back to the
system Python plus your `--user` packages.

**WSGI configuration file** — click the link and replace the whole file with the
contents of `pythonanywhere_wsgi.py` from this repo, editing `USERNAME` and
`PROJECT` at the top.

**Static files** — add these two mappings:

| URL | Directory |
| --- | --- |
| `/static/` | `/home/yourname/hair-saloon/staticfiles` |
| `/media/` | `/home/yourname/hair-saloon/media` |

(WhiteNoise also serves `/static/` on its own, so the first mapping is belt and
braces; `/media/` is what makes uploaded calendar photos appear.)

Hit **Reload**, then open `https://yourname.pythonanywhere.com/`.

## 6. Email

Set the `EMAIL_*` values in `.env` and reload. With Gmail, create an
[App Password](https://myaccount.google.com/apppasswords) — your normal password
will not work:

```ini
EMAIL_HOST=smtp.gmail.com
EMAIL_PORT=587
EMAIL_USE_TLS=True
EMAIL_HOST_USER=salon@gmail.com
EMAIL_HOST_PASSWORD=abcd efgh ijkl mnop
DEFAULT_FROM_EMAIL=Your Salon <salon@gmail.com>
```

> **Free PythonAnywhere accounts cannot open outbound SMTP connections.** The
> site works, but no email leaves the server: the owner must accept and decline
> from `/manage/` instead of from an email. A paid account lifts the restriction.
> Leaving `EMAIL_HOST` empty logs each email to the server log instead of
> sending it, which is a useful halfway house while testing.

## 7. Connect Google Calendar

Log in and open **`https://yourname.pythonanywhere.com/manage/google/`**, then
follow **Connecting Google Calendar** in [README.md](README.md): paste the Client
ID and Client Secret, connect the Google account, and add the calendars you want
on the homepage.

The one thing that must match exactly is the **Authorised redirect URI** on the
OAuth client in the Google Cloud console:

```
https://yourname.pythonanywhere.com/manage/google/callback/
```

The setup page shows the exact URI with a **Copy** button — use it. Note that
PythonAnywhere terminates HTTPS at its proxy and forwards plain HTTP internally,
so Django can build `http://` URLs; the app forces `https://` on the callback for
exactly this reason. If you still get `redirect_uri_mismatch`, the URI registered
at Google differs from the one displayed on that page.

While the OAuth consent screen is unpublished, add every address you will connect
under **Test users**, or Google returns `access_denied`.

Nothing has to be uploaded to the server — the credentials are entered in the
browser and stored encrypted in the database. To verify from a console:

```bash
cd ~/hair-saloon && python3.11 manage.py check_google
```

## 8. Live chat (optional)

Nothing to install — but if you switch it on, schedule the transcript purge.
Chat transcripts are personal data and nothing deletes them without this job.

*Tasks* tab → **Create a scheduled task**, daily:

```
cd ~/hair-saloon && python3.11 manage.py purge_old_chats
```

Free accounts get one daily task, which is exactly enough.

Two things to keep an eye on:

- **CPU seconds.** Chat polls over HTTP (PythonAnywhere has no WebSockets on
  these plans). An open chat window is roughly one small request every
  5 seconds. If your allowance is tight, raise **Chat settings → poll seconds**.
- **Leaving it on unattended.** By default chat follows your business hours, so
  it closes itself when the salon does. If you turn *follow business hours* off,
  remember to use the switch at `/chat/desk/` — otherwise customers sit watching
  "waiting for someone to join" with nobody there.

---

## Updating later

```bash
cd ~/hair-saloon
git pull
pip3.11 install --user -r requirements.txt
python3.11 manage.py migrate
python3.11 manage.py collectstatic --noinput
```

Then **Reload** on the Web tab.

## When something breaks

- **`ModuleNotFoundError: No module named 'django'` (or `googleapiclient`) in the
  error log, but the console works** → the Web tab's Python version does not
  match the `pip3.11` you installed with. Make them the same and reload.
- **`error: externally-managed-environment` or a permissions error from pip** →
  you left out `--user`.
- **500 with `DEBUG=False`** → *Web → Error log*. Nine times out of ten it is
  `DJANGO_ALLOWED_HOSTS`.
- **CSRF error on the booking form** → `DJANGO_CSRF_TRUSTED_ORIGINS` must include
  `https://yourname.pythonanywhere.com`.
- **Site unstyled** → re-run `collectstatic` and check the static mapping.
- **Accept / decline links point at 127.0.0.1** → `SITE_BASE_URL` is wrong.
  Existing links stay broken; fix it and re-send by re-saving the appointment.
- **`redirect_uri_mismatch` when connecting Google** → the URI registered on the
  OAuth client must equal the one shown on `/manage/google/`, character for
  character (scheme, host, trailing slash).
- **`access_denied` on the consent screen** → add that Google address under
  **Test users** on the OAuth consent screen, or publish the app.
- **"Google did not return a refresh token"** → the account already granted
  access. Remove the app at <https://myaccount.google.com/permissions> and
  connect again.
- **Google connection dies after changing `DJANGO_SECRET_KEY`** → the stored
  tokens are encrypted with a key derived from it. Reconnect the account on
  `/manage/google/`.
- **`Google API error 404`** → the calendar ID is wrong, or the calendar has not
  been shared with the service-account address.
- **`Google API error 403`** → shared, but without *Make changes to events*.
- **Bookings save but do not appear in Google** → look at *Google sync error* on
  the appointment in the admin, fix the cause, then use the **Re-sync** action.
- **A free account's scheduled tasks / SMTP limits** → both are account
  restrictions, not bugs in the app.
