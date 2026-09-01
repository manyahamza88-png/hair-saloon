# Hair Salon booking system

A small Django booking site for a hair salon, built to run on **PythonAnywhere**.

The salon owner configures any number of **named Google Calendars** (one per
stylist, chair or room). Each one appears as a card on the homepage; customers
click it, pick a free time and reserve. The Google account that owns that
calendar gets an email with **Accept** and **Decline** buttons, and the event is
written straight into their Google Calendar.

---

## What it does

**For customers**
- Homepage lists every configured calendar with its next free slot.
- Pick a service, a day, then a time. Only genuinely free times are shown.
- Booking confirmation by email, with a self-service cancellation link.

**For the salon owner (in `/admin/`)**
- Add and remove calendars, each with its own name, photo, colour and blurb.
- Attach Google credentials and test the connection with one click.
- Set business days and hours, salon-wide or per calendar, with lunch breaks.
- Mark vacation and one-off closures, salon-wide or for one stylist.
- Approve or decline requests, and re-sync anything Google missed.
- Booking rules: slot interval, appointment length, turnaround buffer,
  minimum notice, how far ahead people may book, approval on or off.

**Google Calendar integration**
- A pending request lands on the calendar immediately as a *tentative* event,
  so the slot is visibly held.
- Accepting turns it into a confirmed event and invites the customer.
- Declining or cancelling removes it again.
- Slots also respect anything already in that Google Calendar (free/busy), so
  a dentist appointment the stylist added by hand blocks online bookings.
- If Google is unreachable, bookings still work: the error is recorded and
  shown in the admin, and **Re-sync** replays it later.

---

## Quick start (local)

```bash
pip install -r requirements.txt

python manage.py migrate
python manage.py init_salon --name "Studio Lumiere" --timezone Europe/Berlin --demo
python manage.py createsuperuser
python manage.py runserver
```

Then open <http://127.0.0.1:8000/> for the shop and
<http://127.0.0.1:8000/admin/> to configure it.

Without an `EMAIL_HOST`, emails are printed to the console instead of being
sent — so you can walk through the whole accept/decline flow before touching
SMTP. Copy `.env.example` to `.env` when you are ready to configure it properly.

> The repository ships with one sample calendar (*Maria – Colour & Cuts*) and a
> few sample services so the homepage is not empty on first run. Delete them in
> the admin once you add your own.

---

## Connecting a Google Calendar

The recommended route is a **service account** — no browser round-trip, nothing
expires, and it works unattended on PythonAnywhere.

### 1. Create the credential in Google Cloud

1. <https://console.cloud.google.com/> → create a project.
2. **APIs & Services → Library** → enable **Google Calendar API**.
3. **APIs & Services → Credentials → Create credentials → Service account.**
4. Open the new service account → **Keys → Add key → Create new key → JSON**.
   A `.json` file downloads.

### 2. Store it in the salon admin

*Admin → Google credentials → Add.* Give it a name, leave the type as
**Service account**, paste the whole JSON file into the field, and save.

The page now shows a **Share calendars with** address, something like
`salon-bot@your-project.iam.gserviceaccount.com`.

### 3. Share each calendar with it

In Google Calendar → hover the calendar → **⋮ → Settings and sharing**:

- Under **Share with specific people**, add that address with the permission
  **Make changes to events**.
- Under **Integrate calendar**, copy the **Calendar ID**.

### 4. Create the calendar in the salon admin

*Admin → Calendars → Add:*

| Field | What to put in it |
| --- | --- |
| **Name** | What customers see, e.g. *Maria – Colour & Cuts* |
| **Google calendar ID** | The ID you copied (or `primary`, or an email address) |
| **Owner email** | The Google account that receives accept / decline emails |
| **Credential** | The credential from step 2 |

Save, then select the calendar in the list and run the action **Test connection
to this calendar**. A green message means you are done — it now appears on the
homepage.

From a console you can check everything at once:

```bash
python manage.py check_google
```

### Alternative: OAuth instead of a service account

If a stylist would rather connect their personal Gmail calendar than share it,
create a **Desktop app** OAuth client in Google Cloud, download the client
secrets, and run this **on a machine with a browser**:

```bash
python manage.py connect_google --name "Maria's Google" --client-secrets client_secret.json
```

It stores the refresh token as a credential, and prints it so you can paste it
into the same field on the server.

> A plain **API key** cannot create events or read a private calendar; Google
> only accepts it for public, read-only access. There is a field for one, but
> use a service account for anything real.

---

## How availability is calculated

A time is offered only when all of these hold:

1. the weekday is open (a calendar's own hours override the salon default);
2. it is not inside a vacation or closure entry;
3. it does not overlap a pending or confirmed booking, plus the turnaround
   buffer;
4. it does not overlap a busy block in the linked Google Calendar;
5. it respects the minimum notice and the maximum booking horizon.

Every slot is re-checked at submit time, so two people clicking the same time
cannot both get it — the second one is told the slot has just gone.

---

## Layout

```
hairsaloon/settings.py         env-driven settings, SQLite or MySQL
booking/models.py              salon settings, credentials, calendars,
                               services, hours, time off, appointments
booking/availability.py        slot generation and the authoritative re-check
booking/google_calendar.py     Google Calendar API wrapper (fails soft)
booking/notifications.py       transactional email
booking/services.py            create / confirm / decline / cancel
booking/admin.py               the owner's control panel
booking/views.py               public pages, JSON slot API, staff dashboard
booking/tests.py               42 tests, no network access needed
```

## Tests

```bash
python manage.py test booking
```

Covers slot generation, breaks, buffers, vacation, per-calendar overrides,
double booking, the honeypot, the signed accept/decline links (including that a
mail client prefetching a link cannot accept a booking), and the Google sync
paths with a stubbed API client.

## Deployment

See [DEPLOY_PYTHONANYWHERE.md](DEPLOY_PYTHONANYWHERE.md).
