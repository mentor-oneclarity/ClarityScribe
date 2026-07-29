# Google Meet Workspace Add-on

This guide publishes the bot as a Google Workspace Add-on that appears as a sidebar
inside Google Meet. Users click one button, the bot joins, and a live transcript
appears in the sidebar.

## What You'll Build

```
User opens Google Meet
        ↓
Sidebar appears (your Add-on panel)
        ↓  HTTP POST /bot/join
FastAPI backend  →  https://your-backend.example.com
        ↓
Bot joins the same meeting, scrapes captions and audio
        ↓
Transcript visible in the sidebar + the full dashboard
```

## Prerequisites

- A Google account (Workspace access recommended; a free Gmail account works for
  testing an internal/private install)
- Your backend deployed and reachable — see [docs/deployment.md](deployment.md).
  **Google Workspace Add-ons require HTTPS** for anything beyond your own test
  installs, so put the backend behind a domain with a TLS certificate before
  submitting anything for review.
- A Google Cloud project (the free tier is enough)

---

## Part 1 — Google Cloud Project Setup

### Step 1 — Create a project

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Click the project dropdown → **New Project**
3. Name it (e.g. `meeting-bot-addon`) → **Create**
4. Make sure the new project is selected in the dropdown

### Step 2 — Enable required APIs

Go to **APIs & Services → Enable APIs and Services** and enable:

| API | Why |
|---|---|
| Google Workspace Marketplace SDK | To publish the Add-on |
| Apps Script API | To run the sidebar code |
| Google Meet API | To read meeting info |

Or via Cloud Shell:

```bash
gcloud config set project meeting-bot-addon
gcloud services enable \
  appsmarket-component.googleapis.com \
  script.googleapis.com \
  meet.googleapis.com
```

### Step 3 — Configure the OAuth consent screen

1. **APIs & Services → OAuth consent screen**
2. Select **External** → **Create**
3. Fill in: App name, support email, developer contact email
4. **Save and Continue** (Scopes and Test Users can be skipped for now)

---

## Part 2 — Create the Apps Script Add-on

### Step 4 — Open Apps Script

Go to [script.google.com](https://script.google.com) → **New Project**, and rename it
(click "Untitled project" at the top).

### Step 5 — Set up `appsscript.json`

**Project Settings** (gear icon) → check **Show `appsscript.json` manifest file** →
back in the **Editor**, open `appsscript.json` and replace its contents:

```json
{
  "timeZone": "Etc/UTC",
  "dependencies": {},
  "exceptionLogging": "STACKDRIVER",
  "runtimeVersion": "V8",
  "addOns": {
    "common": {
      "name": "Meeting Bot",
      "logoUrl": "https://www.gstatic.com/images/branding/product/1x/googleg_48dp.png",
      "layoutProperties": {
        "primaryColor": "#1a73e8",
        "secondaryColor": "#174ea6"
      },
      "openLinkUrlPrefixes": [
        "https://your-backend.example.com"
      ]
    },
    "meet": {
      "onJoinMeeting": {
        "runFunction": "onMeetJoin"
      },
      "sidePanelNavigation": {
        "runFunction": "buildSidePanel"
      }
    }
  },
  "oauthScopes": [
    "https://www.googleapis.com/auth/meetings.space.readonly",
    "https://www.googleapis.com/auth/script.external_request",
    "https://www.googleapis.com/auth/userinfo.email"
  ]
}
```

Set `timeZone` to whatever is appropriate for your deployment.

### Step 6 — Write the Add-on code

Open `Code.gs`, delete the contents, and paste:

```javascript
// ── Config ────────────────────────────────────────────────────────────────────
var BACKEND = "https://your-backend.example.com";

// ── Triggered automatically when a user joins a Meet ──────────────────────────
function onMeetJoin(e) {
  var meetCode = e.meet.meetingCode;
  var meetUrl  = "https://meet.google.com/" + meetCode;

  // Tell the bot to join (fire and forget — don't block the UI)
  try {
    UrlFetchApp.fetch(BACKEND + "/bot/join", {
      method          : "post",
      contentType     : "application/json",
      payload         : JSON.stringify({ meet_url: meetUrl, title: "Meet: " + meetCode }),
      muteHttpExceptions: true,
      followRedirects : true
    });
  } catch (err) {
    Logger.log("Bot join error: " + err);
  }

  return buildSidePanel(e);
}

// ── Sidebar UI ──────────────────────────────────────────────────────────────────
function buildSidePanel(e) {
  var meetCode = e.meet.meetingCode;
  var meetUrl  = "https://meet.google.com/" + meetCode;

  var isRunning = false;
  try {
    var resp   = UrlFetchApp.fetch(BACKEND + "/bot/status", { muteHttpExceptions: true });
    var status = JSON.parse(resp.getContentText());
    isRunning  = status[meetCode] === "running";
  } catch (_) {}

  var card = CardService.newCardBuilder()
    .setHeader(
      CardService.newCardHeader()
        .setTitle("Meeting Bot")
        .setSubtitle(isRunning ? "Recording in progress" : "Bot not active")
        .setImageUrl("https://www.gstatic.com/images/branding/product/1x/googleg_48dp.png")
    );

  var statusSection = CardService.newCardSection()
    .setHeader("Status")
    .addWidget(
      CardService.newDecoratedText()
        .setText(isRunning ? "Bot is in this meeting and recording." : "Bot is not in this meeting yet.")
        .setWrapText(true)
    )
    .addWidget(
      CardService.newDecoratedText().setText("Meet code: " + meetCode).setWrapText(false)
    );
  card.addSection(statusSection);

  var actionsSection = CardService.newCardSection().setHeader("Actions");

  if (!isRunning) {
    actionsSection.addWidget(
      CardService.newButtonSet().addButton(
        CardService.newTextButton()
          .setText("Start Recording")
          .setTextButtonStyle(CardService.TextButtonStyle.FILLED)
          .setOnClickAction(
            CardService.newAction().setFunctionName("startBot")
              .setParameters({ meet_code: meetCode, meet_url: meetUrl })
          )
      )
    );
  } else {
    actionsSection.addWidget(
      CardService.newButtonSet().addButton(
        CardService.newTextButton()
          .setText("Stop Recording")
          .setTextButtonStyle(CardService.TextButtonStyle.FILLED)
          .setOnClickAction(
            CardService.newAction().setFunctionName("stopBot")
              .setParameters({ meet_code: meetCode })
          )
      )
    );
  }

  actionsSection.addWidget(
    CardService.newButtonSet().addButton(
      CardService.newTextButton()
        .setText("View Live Transcript")
        .setOpenLink(
          CardService.newOpenLink().setUrl(BACKEND).setOpenAs(CardService.OpenAs.FULL_SIZE)
        )
    )
  );

  actionsSection.addWidget(
    CardService.newButtonSet().addButton(
      CardService.newTextButton()
        .setText("Refresh Status")
        .setOnClickAction(CardService.newAction().setFunctionName("buildSidePanel"))
    )
  );
  card.addSection(actionsSection);

  card.addSection(
    CardService.newCardSection().addWidget(
      CardService.newDecoratedText()
        .setText("The bot joins the meeting and transcribes speech in real time.")
        .setWrapText(true)
    )
  );

  return [card.build()];
}

// ── Start bot action ──────────────────────────────────────────────────────────
function startBot(e) {
  var meetCode = e.parameters.meet_code;
  var meetUrl  = e.parameters.meet_url;

  try {
    var resp = UrlFetchApp.fetch(BACKEND + "/bot/join", {
      method            : "post",
      contentType       : "application/json",
      payload           : JSON.stringify({ meet_url: meetUrl, title: "Meet: " + meetCode }),
      muteHttpExceptions: true
    });
    var code = resp.getResponseCode();
    if (code === 202) {
      return _notify("Bot is joining the meeting…", e);
    } else if (code === 409) {
      return _notify("Bot is already running for this meeting.", e);
    } else {
      return _notify("Error starting bot (HTTP " + code + ")", e);
    }
  } catch (err) {
    return _notify("Failed to reach backend: " + err, e);
  }
}

// ── Stop bot action ─────────────────────────────────────────────────────────────
function stopBot(e) {
  var meetCode = e.parameters.meet_code;
  try {
    UrlFetchApp.fetch(BACKEND + "/bot/" + meetCode + "/stop", {
      method            : "delete",
      muteHttpExceptions: true
    });
    return _notify("Bot stopped for " + meetCode, e);
  } catch (err) {
    return _notify("Failed to stop bot: " + err, e);
  }
}

// ── Helper: show notification + refresh card ────────────────────────────────────
function _notify(message, e) {
  var notification = CardService.newNotification().setText(message);
  return CardService.newActionResponseBuilder()
    .setNotification(notification)
    .setNavigation(CardService.newNavigation().updateCard(buildSidePanel(e)[0]))
    .build();
}
```

If your backend has `BOT_API_KEY` set (see [docs/deployment.md](deployment.md#environment-variables)),
add an `X-API-Key` header to each `UrlFetchApp.fetch()` call above.

### Step 7 — Save

`Ctrl+S` / `Cmd+S`. Confirm both `appsscript.json` and `Code.gs` are saved.

---

## Part 3 — Deploy the Add-on

### Step 8 — Create a test deployment

1. **Deploy** (top right) → **Test deployments** → **Install** next to your account →
   **Done**.

This installs the Add-on on your own Google account for testing — no review required.

### Step 9 — Test it in Google Meet

1. Go to [meet.google.com](https://meet.google.com), start or join any meeting
2. Click the puzzle-piece icon in the side panel
3. **Meeting Bot** should be listed — click it to open the sidebar

If it doesn't appear, refresh the page; it can take a minute or two after first
install.

### Step 10 — Create a production deployment

1. **Deploy → New Deployment** → type **Add-on**
2. Description: `v1.0`
3. **Deploy**, then copy the **Deployment ID** — you'll need it for the Marketplace
   listing

---

## Part 4 — Publish to Google Workspace Marketplace

### Step 11 — Link Apps Script to your Cloud project

**Project Settings** (gear icon) → **Google Cloud Platform (GCP) Project** →
**Change project** → enter your project number (Cloud Console → Home → Project info) →
**Set project**.

### Step 12 — Configure the Marketplace SDK

1. In Cloud Console, search **Marketplace SDK** → **Google Workspace Marketplace SDK**
   → **Enable**
2. **APIs & Services → Google Workspace Marketplace SDK → App Configuration**, fill in:

| Field | Value |
|---|---|
| App Visibility | Public, or Private for your org only |
| App Type | Google Workspace Add-on |
| Google Workspace Add-on Deployment ID | From Step 10 |
| Developer Name | Your name or company |
| Developer Website | Your website |
| Support URL | Your support page or contact email |

### Step 13 — OAuth scopes

Add exactly these:

```
https://www.googleapis.com/auth/meetings.space.readonly
https://www.googleapis.com/auth/script.external_request
https://www.googleapis.com/auth/userinfo.email
```

### Step 14 — Store listing

**Store Listing** tab:

| Field | Example |
|---|---|
| App name | Meeting Bot |
| Short description | Automatically transcribe and summarize Google Meet meetings |
| Long description | See template below |
| Category | Productivity |
| Language | English |

Long description template:

```
This Add-on automatically joins your Google Meet calls and transcribes them in
real time.

Features:
• One-click recording — no manual setup
• Live transcript visible during the meeting
• Speaker identification
• AI-generated meeting summary after the call

How it works:
1. Install this Add-on
2. Join any Google Meet
3. Click "Start Recording" in the sidebar
4. The bot joins and begins transcribing
5. View the full transcript and summary on your dashboard
```

### Step 15 — Upload icons

Two square PNGs: `icon_128.png` (128×128) and `icon_512.png` (512×512).

### Step 16 — Submit for review

**Publish → Submit for review**. Google typically reviews public Add-ons within
3–7 business days.

---

## Part 5 — Skip Review (Internal / Private Deployment)

For an org-only install with no review wait:

1. Marketplace SDK → App Configuration → set **App Visibility** to **Private**
2. [Google Admin Console](https://admin.google.com) → **Apps → Google Workspace
   Marketplace apps** → **Add app** → search your private app
3. **Install for everyone** or specific groups

Everyone in the org will now see the Add-on in Google Meet automatically.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Sidebar doesn't appear in Meet | Refresh the page; wait a minute or two after first install |
| "Bot join error" in Apps Script logs | Check the `BACKEND` variable in `Code.gs`; confirm the server is up and reachable |
| Bot joins but no transcript appears | Bot's saved session likely expired — re-run `python -m bot.login_once` and update `BOT_SESSION_B64` |
| "Script function not found" error | Make sure `Code.gs` is saved and function names match exactly |
| Add-on missing after install | Clear the browser cache, or try an incognito window |
| HTTP error calling the backend | Google Workspace Add-ons require HTTPS in production — see the note below |

## HTTP vs HTTPS

Google Workspace Add-ons require your backend to be reachable over **HTTPS** before you
can submit for Marketplace review (plain HTTP is fine for local test installs only).
If you're deploying to a bare VM:

1. Point a domain name at your server
2. Get a free TLS certificate (e.g. via Let's Encrypt)
3. Run nginx or Caddy as a reverse proxy in front of the backend

```bash
sudo apt install nginx certbot python3-certbot-nginx -y
sudo certbot --nginx -d your-domain.com
# Configure nginx to proxy 443 → localhost:6050
```

Then update `BACKEND` in `Code.gs` to `https://your-domain.com` before submitting for
review.

## Quick Reference

| Task | Where |
|---|---|
| Edit sidebar code | [script.google.com](https://script.google.com) → your project → `Code.gs` |
| Test without review | Deploy → Test deployments → Install |
| View Apps Script logs | Executions (left sidebar) |
| Update backend URL | Change `BACKEND` in `Code.gs` → redeploy |
| Publish a new version | Deploy → New Deployment → Add-on |
| Check bot status | `https://your-backend.example.com/bot/status` |
| View all meetings / API docs | `https://your-backend.example.com/docs` |
