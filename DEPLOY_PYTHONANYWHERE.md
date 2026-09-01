# Deploying to PythonAnywhere

Roughly fifteen minutes end to end. Replace `yourname` with your PythonAnywhere
username everywhere below.

---

## 1. Get the code onto the server

Open a **Bash console** on the PythonAnywhere *Consoles* tab:

```bash
cd ~
git clone <your-repo-url> hair-saloon      # or upload a zip and unzip it
cd hair-saloon
```

## 2. Virtualenv and dependencies

```bash
mkvirtualenv --python=/usr/bin/python3.11 salon
pip install -r requirements.txt
```

Remember the virtualenv name (`salon`) — the Web tab needs it.

## 3. Settings

```bash
cp .env.example .env
python -c "from django.core.management.utils import get_random_secret_key as k; print(k())"
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
python manage.py migrate
python manage.py init_salon --name "Your Salon" --timezone Europe/Berlin
python manage.py createsuperuser
python manage.py collectstatic --noinput
```

SQLite is fine for one salon. To use PythonAnywhere's MySQL instead, create the
database on the *Databases* tab, `pip install mysqlclient`, fill in the `DB_*`
lines in `.env`, and re-run `migrate`.

## 5. The Web tab

*Web → Add a new web app → **Manual configuration** → Python 3.11.*

| Field | Value |
| --- | --- |
| Source code | `/home/yourname/hair-saloon` |
| Working directory | `/home/yourname/hair-saloon` |
| Virtualenv | `/home/yourname/.virtualenvs/salon` |

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

Follow **Connecting a Google Calendar** in [README.md](README.md), then confirm
it from a Bash console:

```bash
workon salon && cd ~/hair-saloon && python manage.py check_google
```

Nothing has to be uploaded to the server for this — the service-account JSON is
pasted into the admin and lives in the database.

---

## Updating later

```bash
workon salon && cd ~/hair-saloon
git pull
pip install -r requirements.txt
python manage.py migrate
python manage.py collectstatic --noinput
```

Then **Reload** on the Web tab.

## When something breaks

- **500 with `DEBUG=False`** → *Web → Error log*. Nine times out of ten it is
  `DJANGO_ALLOWED_HOSTS`.
- **CSRF error on the booking form** → `DJANGO_CSRF_TRUSTED_ORIGINS` must include
  `https://yourname.pythonanywhere.com`.
- **Site unstyled** → re-run `collectstatic` and check the static mapping.
- **Accept / decline links point at 127.0.0.1** → `SITE_BASE_URL` is wrong.
  Existing links stay broken; fix it and re-send by re-saving the appointment.
- **`Google API error 404`** → the calendar ID is wrong, or the calendar has not
  been shared with the service-account address.
- **`Google API error 403`** → shared, but without *Make changes to events*.
- **Bookings save but do not appear in Google** → look at *Google sync error* on
  the appointment in the admin, fix the cause, then use the **Re-sync** action.
- **A free account's scheduled tasks / SMTP limits** → both are account
  restrictions, not bugs in the app.
