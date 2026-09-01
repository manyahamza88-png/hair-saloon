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
- Live chat bubble for quick questions, when the salon has it switched on.

**For the salon owner (in `/admin/` and `/manage/google/`)**
- Link a Google account from the admin panel with a Client ID and Client Secret.
- Pick which of that account's calendars go on the homepage, and name each one.
- Add and remove calendars, each with its own name, photo, colour and blurb.
- Set business days and hours, salon-wide or per calendar, with lunch breaks.
- Mark vacation and one-off closures, salon-wide or for one stylist.
- Approve or decline requests, and re-sync anything Google missed.
- Booking rules: slot interval, appointment length, turnaround buffer,
  minimum notice, how far ahead people may book, approval on or off.
- Answer live chats from a staff desk, and switch chat off when nobody is free.

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

## Connecting Google Calendar

Everything happens on one page: **`/manage/google/`** (also linked as *Google* in
the header and from the dashboard). A superuser pastes a Client ID and Client
Secret, links a Google account through the browser, and then picks which of that
account's calendars appear on the homepage.

### 1. Create the OAuth client in Google Cloud

1. <https://console.cloud.google.com/> → create a project.
2. **APIs & Services → Library** → enable **Google Calendar API**.
3. **OAuth consent screen** → External → add your own address under
   **Test users** while the app is still unpublished.
4. **Credentials → Create credentials → OAuth client ID → Web application.**
5. Under **Authorised redirect URIs**, add exactly:

   ```
   https://yourname.pythonanywhere.com/manage/google/callback/
   ```

   The Google setup page displays this URI with a **Copy** button — use that
   rather than typing it. It must match character for character, including the
   scheme and the trailing slash, or Google returns `redirect_uri_mismatch`.

6. Copy the **Client ID** and **Client Secret**.

### 2. Save them in the admin

Open **`/manage/google/`** → *1. Google Client ID & Secret* → paste both → **Save
client**.

The secret is encrypted at rest (Fernet, keyed off `SECRET_KEY`) and is never
displayed again — leave the box blank when re-saving to keep the stored one.
`.env` values (`GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET`) act as a
fallback; the database wins as soon as a Client ID is entered, and the two are
never mixed.

### 3. Link the Google account

Click **Connect a Google account**. Google's consent screen appears, you sign in
and approve, and you land back on the setup page showing the connected address.

That grants this app **full read and write access** to that account's calendars.
The refresh token is stored encrypted; nothing is written into your Google
account, and no calendar sharing is involved.

To hand over to a different account later, use **Reconnect / switch account**.
**Disconnect** forgets the tokens (optionally revoking them at Google too) while
leaving your calendars and bookings intact.

### 4. Add calendars to the homepage

Section 3 of the page lists every calendar in the connected account. For each one
you want to offer, type the name customers should see — it does not have to match
the Google name — set the address that should receive the accept / decline
emails, and click **Add**.

It immediately becomes a card on the homepage. Opening hours, a photo, a colour
and a description can be set afterwards in *Admin → Calendars*.

Calendars the account can only read are shown but cannot be added: this app has
to be able to write bookings into them.

### Alternative: service account

The service-account route still works and needs no browser round-trip, which
suits an unattended server. In *Admin → Google credentials*, paste a service
account JSON key; then in *Admin → Calendars* add an entry with the **Calendar
ID left empty** and run the action **Create a new Google calendar**. The service
account creates and owns the calendar, then shares it out to the owner email.

Two things to know about that route:

- A service account cannot add the customer as a Google **attendee** (Google
  reserves that for real users and Workspace domain-wide delegation). The app
  detects this and puts the customer's details in the event description instead.
  The OAuth route above *can* invite attendees.
- To point at a calendar that already exists, you must share it with the service
  account address manually and paste its Calendar ID.

### What about a plain API key?

It cannot run this app. Google accepts API keys only for *public, read-only*
calendar data — never for creating events or reading a private calendar,
whatever scopes are requested. Use the OAuth client above.

## Live chat

A floating chat bubble on every customer page, and a staff desk at
**`/chat/desk/`** (linked as *Chat* in the header) to answer from.

### Two controls

| Control | Where | What it does |
| --- | --- | --- |
| **Switched on** | Desk, or *Admin → Chat settings* | The master switch. Off means no chat at all, whatever the clock says. |
| **Follow business hours** | *Admin → Chat settings* | When ticked, chat is additionally limited to the salon's opening hours. |

The switch always wins. Ticking *follow business hours* narrows when chat is
offered; it can never turn chat on while the switch is off.

Chat is **off by default**. With both on — the normal setup — the bubble appears
by itself when you open and disappears when you close, and you still have the
switch for "we are slammed, stop the chats" without touching your hours.

Business hours come from the same
*Admin → Business hours* and *Admin → Time off* rows that drive the booking
calendar, lunch breaks and vacation included. One place to edit, and chat
follows. If no hours are configured at all, chat stays available rather than
silently never appearing.

Two deliberate differences between the two controls:

- **Closing time never cuts a live chat off mid-sentence.** Once a conversation
  is accepted it can be finished, and only *new* chats are refused after hours.
- **The switch does close everything**, immediately — that is the point of it.

Outside opening hours the customer is told when you are back ("We are back
tomorrow at 09:00") rather than just seeing a dead bubble. Whether the bubble
stays visible at all is *show when offline*.

Transcripts are kept whichever way chat is turned off.

### How a conversation goes

1. The customer clicks the bubble (only shown when chat is available), gives a
   first name and their question.
2. The request appears on the staff desk as **waiting**, with the question and
   the page they were on ("from /book/maria/") so you have context before you
   answer.
3. Staff **Accept** — the customer sees a greeting and the chat goes live — or
   **Too busy**, which sends a polite apology and frees them to book online.
4. Either side can end the chat. The customer's widget resets.

### Why polling, not WebSockets

PythonAnywhere does not offer WebSockets on the plans a small salon would use,
so the browser asks for new messages on a timer instead. To keep request counts
(and CPU seconds) down, the rate adapts: fast while the chat window is open,
slower when it is closed, backing off further when the tab is hidden or nothing
has happened for a while. The base interval is **Chat settings → poll seconds**
(default 5) — raise it if you are watching your CPU allowance.

### Privacy

Chat transcripts are personal data. They are deleted automatically after
**Chat settings → retention days** (default 365) by:

```bash
python manage.py purge_old_chats            # honours the configured window
python manage.py purge_old_chats --dry-run  # show what would go
```

Run it daily — on PythonAnywhere via the *Tasks* tab. Nothing deletes itself
without it.

Wording (welcome text, greeting, the busy apology, the offline message) is all
editable in *Admin → Chat settings*, and full transcripts are readable in
*Admin → Conversations*.

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
booking/google_oauth.py        browser OAuth: link a Google account from admin
hairsaloon/crypto.py           Fernet encryption for tokens and secrets
booking/notifications.py       transactional email
booking/services.py            create / confirm / decline / cancel, provisioning
booking/admin.py               the owner's control panel
booking/views.py               public pages, JSON slot API, staff dashboard
booking/tests.py               65 tests, no network access needed
chat/models.py                 chat settings, conversations, messages
chat/views.py                  widget + staff desk JSON endpoints
chat/availability.py           when chat is offered: switch + business hours
chat/tests.py                  41 tests (availability, isolation, retention)
static/js/chat.js              the customer widget (adaptive polling)
```

## Tests

```bash
python manage.py test          # 106 tests
```

Covers slot generation, breaks, buffers, vacation, per-calendar overrides,
double booking, the honeypot, the signed accept/decline links (including that a
mail client prefetching a link cannot accept a booking), calendar provisioning
(and that a failed share does not lose the calendar just created), the
attendee restriction on service accounts, the OAuth admin flow (PKCE, secret
encryption, the env-vs-database fallback, reconnecting without a fresh refresh
token, and the forced-HTTPS callback), and the Google sync paths with a stubbed
API client.

## Deployment

See [DEPLOY_PYTHONANYWHERE.md](DEPLOY_PYTHONANYWHERE.md).
