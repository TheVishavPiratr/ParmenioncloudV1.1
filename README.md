# VisionPulse Cloud (ParmenioncloudV1.1)

The cloud backend for the **VisionPulse** CCTV security system.

VisionPulse has three parts:

- **The app**, which the owner uses on a phone, tablet or browser.
- **The edge box**, a small computer at the owner's site (home, shop, office)
  that runs the Edge Agent. It sits on the same network as the cameras,
  watches the video and spots people and faces.
- **The cloud** (this project), which connects the two. It stores accounts,
  settings, footage and alerts, and sends instructions to the edge boxes.

---

# Part 1: How it works

## The journey, start to finish

1. **Create an account.** The owner signs up with an email address and confirms
   it with a code sent by email.
2. **Sign in on a phone.** The phone becomes a *linked device*. It stays signed
   in and can receive alerts.
3. **Add an edge box.** The owner creates a "site" in the app and gets a one-time
   setup code. They enter that code on the edge box, and the box connects to the
   cloud.
4. **Find the cameras.** The edge box scans its local network and reports every
   camera it finds. In the app the owner picks which ones to use and enters their
   login details. The cloud sends the new camera list to the box, and the box
   starts watching them.
5. **Teach it faces (optional).** The owner adds people to a gallery (family,
   staff, visitors, or people they never want on the premises) with a few photos
   each. The edge box turns those photos into face data so it can recognise them.
6. **Something happens.** A camera sees motion, a person, a known face, an
   unknown face, or someone tampering with it. The edge box uploads a snapshot or
   short clip and reports the event to the cloud.
7. **The owner is alerted.** The cloud decides how serious the event is and
   alerts the owner in the app, by push notification and/or email. Seeing a
   *blocked* person is always treated as critical.
8. **Review.** The owner browses the timeline of events, watches the footage and
   marks events as reviewed.
9. **Everything is recorded.** Every important action (sign-ins, new devices,
   camera changes, footage viewed) goes into the owner's activity log.
10. **Leaving.** The owner can delete the account, which removes all their data
    from the cloud.

Old data is cleaned up automatically: footage after 30 days, events and
notifications after 90 days, and the activity log after a year (all of these
can be configured).

## The modules

The cloud is split into ten modules. Each one looks after one part of the job.

### 1. Auth: accounts and sign-in
Handles everything about the account itself. A person signs up and confirms their
email with a 6-digit code. After that they can sign in. If they forget their
password, a reset code is emailed to them. Changing the password signs the
account out everywhere, and the owner gets a security alert about it.

### 2. Device Link: phones, tablets and browsers
Every phone, tablet or browser the owner signs in on is remembered as a
*linked device*, so they don't have to sign in again each time. The owner can
see the list of devices, rename them, sign out a lost phone remotely, or sign
out every other device at once. This module also stores where push
notifications should go for each device. A new sign-in sends a security alert.
If the same sign-in is used from two places (a sign it may have been stolen),
that device is signed out and the owner is warned.

### 3. Edge Agent: the on-site boxes
Manages the edge boxes. The owner creates a site and gets a one-time setup code,
valid for 24 hours. The box uses it to join the account. From then on the box
checks in every 30 seconds. If it misses about 90 seconds of check-ins, it shows
as offline. The box also keeps a live connection open to the cloud, which the
cloud uses to send it instructions such as "your settings changed" or "scan for
cameras now". If the box is offline, instructions wait and are delivered when it
reconnects. The owner can rename a box, issue a new setup code (for a
replacement box), revoke it, or delete it.

### 4. Cameras: which cameras to watch
Keeps the list of cameras for each edge box (up to 64 per box). A camera
appears in one of two ways: the box finds it during a network scan, or the owner
adds it by hand. A camera that was found by a scan waits for the owner to
*adopt* it (set it up) or *ignore* it (so later scans don't offer it again).
Camera passwords are stored encrypted. Whenever cameras change, the cloud tells
the box to fetch its new settings. The box keeps reporting whether each camera
is streaming properly or has an error.

### 5. Gallery: the people to recognise
The owner's list of known people, each with a category: **family**, **staff**,
**visitor** or **blocked**. Each person can have up to 10 photos. Because face
photos are sensitive personal data, the owner must confirm they have consent
before adding someone. The edge box downloads new photos, turns each one into
face data, and sends that back. If a photo has no usable face, it is marked as
rejected. Face data worked out by one box is shared with the owner's other boxes,
so the work only happens once.

### 6. Media Access: snapshots and video clips
Stores the snapshots and clips the edge boxes upload. Each file is checked
after upload to make sure it arrived complete and unchanged. Once that's
confirmed, the box can delete its own copy. To view footage, the app asks for a
short-lived viewing link (valid about 10 minutes), so the files are never
openly reachable. Videos can be scrubbed and seeked. Files are deleted
automatically after the retention period.

### 7. Events: what the cameras saw
The timeline. The edge box reports each event (motion, a person, a recognised
face, an unknown face, or camera tampering) with its snapshot and, later, a clip.
The cloud decides how serious each event is:
- **critical**: a *blocked* person was recognised
- **warning**: a person, an unknown face, or tampering
- **info**: motion, or a known (non-blocked) face

The cloud then alerts the owner, without flooding them. The same kind of event
on the same camera alerts at most once a minute (every 15 seconds if critical).
Events that arrive more than 10 minutes late are saved but don't alert. The
owner can filter the timeline, mark events as reviewed, or delete them.

### 8. Notifications: alerts to the owner
Every alert lands in the in-app inbox (with an unread badge). Depending on the
owner's settings, it is also sent as a push notification to their devices
and/or by email. Settings are chosen separately for each category of alert:
- **security**: sign-ins, password changes, suspicious sessions
- **system**: edge boxes and cameras
- **detection**: people and motion seen by the cameras

The owner can send a test alert to check that push notifications work.

### 9. Audit: the activity log
A read-only history of everything important on the account: sign-ins
(including failed attempts), devices linked and removed, boxes added, cameras
changed, footage viewed, and gallery changes. Each entry says who did it (the
owner, an edge box, or someone not signed in), when it happened, and from where.

### 10. Account: deleting everything
Lets the owner permanently delete their account after re-entering their
password. Everything is removed in a safe order: edge boxes are disconnected
first, then footage and photos, then the activity log, and finally the account
itself. A confirmation email is sent afterwards.

---

# Part 2: Technical reference

## Stack

Python, FastAPI, SQLAlchemy 2 (async) on PostgreSQL, Alembic migrations,
Argon2 password hashing, JWT login tokens, Fernet-encrypted camera passwords,
Firebase Cloud Messaging for push. See `requirement.txt`.

## Architecture

A modular monolith: one FastAPI app (`api/`) split into feature modules under
`api/modules/`. Shared plumbing (settings, database session, file storage,
signed links) lives in `api/core/`.

Each module has `router.py`, `schemas.py`, `models.py`, `service.py` and
`repository.py`. `account` owns no tables, so it has only a router, schemas, and
a service that calls the other modules.

```
router -> service -> repository -> database
```

- Only `repository.py` may talk to the database.
- Modules call each other only through their `service.py`, never through another
  module's router, repository or models.
- Exception: a module may have a `dependencies.py` holding FastAPI dependencies
  that other modules' routers use, e.g. `auth.dependencies.CurrentUser`.
- Database-agnostic: generic SQLAlchemy types only, no raw SQL, no
  vendor-specific column types. The database URL comes only from `.env`.

Background jobs started with the app delete expired media, audit entries,
notifications and events.

## Authentication

There are three kinds of caller:

| Caller | How it authenticates | Used on |
|---|---|---|
| User (app) | `Authorization: Bearer <access token>` from `/auth/login`, `/devices/login` or `/devices/refresh` | `/auth/me`, `/account`, `/devices`, `/agents`, `/cameras`, `/gallery`, `/media`, `/events`, `/notifications`, `/audit` |
| Edge agent | `Authorization: Bearer <agent token>` from `/agent/enroll` | everything under `/agent` |
| Anyone with a signed link | `?exp=…&sig=…` query string, no header | `/media/files/{id}`, `/gallery/files/{id}` |

Access tokens last `ACCESS_TOKEN_EXPIRE_MINUTES` (default 30). Device refresh
tokens last 60 days and rotate on every use. Reusing an old refresh token signs
that device out. Changing the password or deleting the account invalidates every
token at once.

Interactive API docs are served at `/docs` (Swagger) and `/redoc` while the
server runs.

## API endpoints

### Health
| Method | Path | Description |
|---|---|---|
| GET | `/health` | Server and database check |

### Auth (`/auth`)
| Method | Path | Description |
|---|---|---|
| POST | `/auth/signup` | Create an account; emails a 6-digit verification code |
| POST | `/auth/verify-email` | Confirm the email with the code |
| POST | `/auth/resend-verification` | Send a new code (60 s cooldown; same reply whether or not the account exists) |
| POST | `/auth/forgot-password` | Email a password reset code |
| POST | `/auth/reset-password` | Set a new password with the code; signs out every device |
| POST | `/auth/login` | Email + password → access token |
| GET | `/auth/me` | The signed-in user |

Codes expire after 10 minutes and allow 5 attempts.

### Device Link (`/devices`)
| Method | Path | Description |
|---|---|---|
| POST | `/devices/login` | Sign in from the app and link this device → access + refresh token |
| POST | `/devices/refresh` | Swap the refresh token for a new pair (the old one stops working) |
| POST | `/devices/logout` | Sign out this device (by refresh token) |
| POST | `/devices/logout-all` | Sign out every other device |
| GET | `/devices` | List linked devices (`current` marks the caller) |
| PATCH | `/devices/{device_id}` | Rename, or switch notifications on or off |
| DELETE | `/devices/{device_id}` | Remotely sign out a device |
| PUT | `/devices/{device_id}/push-token` | Register the Firebase push token |
| DELETE | `/devices/{device_id}/push-token` | Remove the push token |

At most 20 devices per user; the oldest is signed out to make room.
Platforms: `android`, `ios`, `web`.

### Edge Agents, user side (`/agents`)
| Method | Path | Description |
|---|---|---|
| POST | `/agents` | Create a site → one-time enrollment token (valid 24 h, shown once) |
| GET | `/agents` | List agents with online status and health |
| GET | `/agents/{agent_id}` | One agent |
| PATCH | `/agents/{agent_id}` | Rename |
| POST | `/agents/{agent_id}/enrollment-token` | New enrollment token (e.g. a replacement box) |
| POST | `/agents/{agent_id}/revoke` | Cancel the agent's token and disconnect it |
| POST | `/agents/{agent_id}/rescan` | Ask the agent to scan for cameras (202) |
| DELETE | `/agents/{agent_id}` | Delete the agent with its cameras |

Agent status: `pending` → `active` → `revoked`.

### Edge Agent, agent side (`/agent`)
| Method | Path | Description |
|---|---|---|
| POST | `/agent/enroll` | Exchange the enrollment token for a long-lived agent token |
| POST | `/agent/heartbeat` | Every 30 s: health, MediaMTX state; reply carries `config_version` |
| GET | `/agent/me` | The agent's own record |
| WS | `/agent/ws` | Command channel (see below) |
| GET | `/agent/config` | Cameras to stream |
| POST | `/agent/cameras/discovered` | Results of a network scan |
| POST | `/agent/cameras/status` | Stream state of each active camera |
| GET | `/agent/gallery?model=<name>` | People and embeddings for the agent's face model |
| GET | `/agent/gallery/faces/{face_id}/image` | A photo still waiting for its embedding |
| POST | `/agent/gallery/embeddings` | Report embeddings, or why a photo has no usable face |
| POST | `/agent/media` | Upload step 1: register a snapshot or clip (size + sha256) |
| PUT | `/agent/media/{media_id}/content` | Upload step 2: the raw file bytes |
| POST | `/agent/events` | Report up to 100 events, oldest first |
| PATCH | `/agent/events/{event_id}` | Attach a snapshot or clip after the fact |

An agent is online when it has sent a heartbeat in the last 90 seconds.

#### WebSocket command channel: `/agent/ws`

Send the agent token in the `Authorization: Bearer` header.

```
cloud -> agent  {"type": "hello", "agent_id", "config_version"}       once, first
                {"type": "command", "id", "command", "payload", "expires_at"}
                {"type": "ack_received", "id"}                          reply to each ack
                {"type": "error", "detail"}                             bad message
agent -> cloud  {"type": "ack", "id", "ok": bool, "error"?}
```

- Commands: `config_changed` (re-fetch `/agent/config` and `/agent/gallery`),
  `rescan_cameras`.
- Close codes: `4401` bad or revoked token (don't retry with it), `4409` replaced
  by a newer connection of the same agent.
- Every open command is re-sent on reconnect, so a command id can repeat.
- If a heartbeat reply shows a newer `config_version`, re-fetch the config
  anyway. This is the safety net for missed commands.

### Cameras (`/cameras`)
| Method | Path | Description |
|---|---|---|
| GET | `/cameras?agent_id=&state=` | List cameras |
| POST | `/cameras` | Add a camera by hand (agent must be enrolled) |
| GET | `/cameras/{camera_id}` | One camera |
| POST | `/cameras/{camera_id}/adopt` | Set up a discovered camera (RTSP URL, credentials) |
| POST | `/cameras/{camera_id}/ignore` | Dismiss a discovered camera |
| PATCH | `/cameras/{camera_id}` | Update name, credentials, recording, detection, enable or disable |
| DELETE | `/cameras/{camera_id}` | Delete |

Camera state: `discovered`, `active`, `disabled`, `ignored`.
Stream state: `unknown`, `streaming`, `error`. At most 64 cameras per agent.

### Gallery (`/gallery`)
| Method | Path | Description |
|---|---|---|
| GET | `/gallery/people?category=` | List people with their photos |
| POST | `/gallery/people` | Add a person (`consent_confirmed: true` required) |
| GET | `/gallery/people/{person_id}` | One person |
| PATCH | `/gallery/people/{person_id}` | Update name, category, notes |
| DELETE | `/gallery/people/{person_id}` | Delete the person, photos and embeddings |
| POST | `/gallery/people/{person_id}/faces` | Upload a photo as the raw request body (JPEG, PNG or WebP, ≤ 5 MB) |
| DELETE | `/gallery/faces/{face_id}` | Delete a photo |
| POST | `/gallery/faces/{face_id}/link` | Signed viewing link (10 min) |
| GET | `/gallery/files/{face_id}?exp=&sig=` | Open the photo through a signed link |

Categories: `family`, `staff`, `visitor`, `blocked`. Photo status: `pending`,
`ready`, `rejected`. Limits: 500 people, 10 photos each.

### Media (`/media`)
| Method | Path | Description |
|---|---|---|
| GET | `/media?camera_id=&kind=&before=&limit=` | List footage, newest first |
| GET | `/media/{media_id}` | One file's details |
| POST | `/media/{media_id}/link` | Signed viewing link (10 min) |
| GET | `/media/files/{media_id}?exp=&sig=` | Stream the file; supports Range requests (seeking) |
| DELETE | `/media/{media_id}` | Delete |

Kinds: `snapshot` (≤ 10 MB) and `clip` (≤ 200 MB, ≤ 1 h). Uploads are checked
against the registered size and SHA-256. At most 100 unfinished uploads per
agent; unfinished uploads expire after 24 h.

### Events (`/events`)
| Method | Path | Description |
|---|---|---|
| GET | `/events?camera_id=&type=&person_id=&severity=&unreviewed_only=&before=&limit=` | Timeline, newest first |
| GET | `/events/unreviewed-count` | Count for a badge |
| POST | `/events/review-all?camera_id=` | Mark all (or one camera's) events reviewed |
| GET | `/events/{event_id}` | One event |
| PATCH | `/events/{event_id}` | Set `reviewed` |
| DELETE | `/events/{event_id}` | Delete the event (its media is kept) |

Types: `motion`, `person_detected`, `face_recognized`, `face_unknown`,
`camera_tampered`. Severity (set by the cloud, never by the agent): `info`,
`warning`, `critical`.

Event reporting rules for agents: batches of up to 100, idempotent per
`client_event_id` (resending a batch returns duplicates, not new events).
Upload the snapshot through `/agent/media` first and pass its id; attach a
clip later with `PATCH /agent/events/{id}`. Alerts are throttled to one per 60 s
(15 s if critical) for the same thing on the same camera. Events older than
10 minutes are stored without alerting.

### Notifications (`/notifications`)
| Method | Path | Description |
|---|---|---|
| GET | `/notifications?unread_only=&before=&limit=` | Inbox, newest first |
| GET | `/notifications/unread-count` | Count for the bell badge |
| POST | `/notifications/read-all` | Mark all read |
| POST | `/notifications/{notification_id}/read` | Mark one read |
| DELETE | `/notifications/{notification_id}` | Delete |
| GET | `/notifications/preferences` | Push/email switches per category |
| PATCH | `/notifications/preferences/{category}` | Change `push` and/or `email` |
| POST | `/notifications/test` | Send a test push to every device with alerts on |

Categories: `security`, `system`, `detection`. The in-app inbox is always on.
For a device to get push alerts, it also needs `notifications_enabled` and a
push token.

### Audit (`/audit`)
| Method | Path | Description |
|---|---|---|
| GET | `/audit?action=&before=&limit=` | Activity log, newest first |

Each entry has an `action`, `success`, `actor` (`user`, `agent`, `anonymous`),
target, details, IP address and user agent.

### Account (`/account`)
| Method | Path | Description |
|---|---|---|
| POST | `/account/delete` | Body `{"password": …}`. Permanently deletes the account and all its data (204) |

### Pagination

List endpoints return newest first with `limit` (1–100, default 50). For the
next page, pass the last item's timestamp (`created_at`, `captured_at` or
`occurred_at`) as `before`.

## Configuration (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | required | SQLAlchemy async URL, e.g. `postgresql+asyncpg://…` |
| `JWT_SECRET` | required | Signs login tokens |
| `CAMERA_CREDENTIALS_KEY` | required | Fernet key for camera passwords |
| `JWT_ALGORITHM` | `HS256` | |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `30` | |
| `MEDIA_STORAGE_DIR` | `media_store` | Where uploaded files are kept |
| `MEDIA_RETENTION_DAYS` | `30` | |
| `EVENT_RETENTION_DAYS` | `90` | |
| `NOTIFICATION_RETENTION_DAYS` | `90` | |
| `AUDIT_RETENTION_DAYS` | `365` | |
| `EMAIL_MODE` | `console` | `console` or `smtp` |
| `PUSH_MODE` | `console` | `console` or `fcm` |

Generate a camera credentials key with:

```
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### Email and push delivery

Both print to the server terminal by default. To send for real, set in `.env`:

```
EMAIL_MODE=smtp
SMTP_HOST=smtp.example.com
SMTP_PORT=587                 # 465 with SMTP_SECURITY=ssl
SMTP_SECURITY=starttls        # starttls | ssl | none
SMTP_USERNAME=...
SMTP_PASSWORD=...
SMTP_FROM=VisionPulse <alerts@example.com>

PUSH_MODE=fcm
FCM_CREDENTIALS_FILE=firebase-service-account.json   # Firebase console > Service accounts
```

## Running

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirement.txt
alembic upgrade head              # apply migrations
uvicorn api.main:app --reload     # GET /health, docs at /docs
pytest -v
ruff check . && ruff format --check .
```
